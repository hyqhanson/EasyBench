"""Extract accessions and metadata from text."""

import re
from typing import Dict, List


def extract_geo_accessions(text: str) -> Dict[str, List[str]]:
    """Extract GEO accessions (GSE, GSM, GPL) from text."""
    gse_pattern = r'\b(GSE\d{3,})\b'
    gsm_pattern = r'\b(GSM\d{3,})\b'
    gpl_pattern = r'\b(GPL\d{3,})\b'

    gse_ids = list(set(re.findall(gse_pattern, text, re.IGNORECASE)))
    gsm_ids = list(set(re.findall(gsm_pattern, text, re.IGNORECASE)))
    gpl_ids = list(set(re.findall(gpl_pattern, text, re.IGNORECASE)))

    return {
        'gse': [x.upper() for x in gse_ids],
        'gsm': [x.upper() for x in gsm_ids],
        'gpl': [x.upper() for x in gpl_ids],
    }


def extract_sra_accessions(text: str) -> List[str]:
    """Extract SRA-related accessions such as SRP/SRR/SRS/ERP/ERS/DRP/DRS."""
    pattern = r'\b((?:SRP|SRR|SRS|ERP|ERS|DRP|DRS)\d{3,})\b'
    ids = list(set(re.findall(pattern, text, re.IGNORECASE)))
    return [x.upper() for x in ids]


def extract_cellxgene_accessions(text: str) -> List[str]:
    """Extract cellxgene dataset slugs from text or URLs."""
    dataset_ids: List[str] = []
    for match in re.findall(r'https?://(?:www\.)?cellxgene\.[^/]+/d/([A-Za-z0-9_\-]+)', text, re.IGNORECASE):
        dataset_ids.append(match)

    if 'cellxgene' in text.lower():
        plain_matches = re.findall(r'\b([A-Za-z0-9_\-]{6,})\b', text)
        for candidate in plain_matches:
            if candidate.lower().startswith('cxg') and candidate not in dataset_ids:
                dataset_ids.append(candidate)

    return list(dict.fromkeys(dataset_ids))


def extract_organism(text: str) -> str:
    """Extract organism/species from text."""
    organisms = {
        'homo sapiens': ['human', 'homo sapiens', 'h. sapiens'],
        'mus musculus': ['mouse', 'mus musculus', 'm. musculus', 'mice'],
        'rattus norvegicus': ['rat', 'rattus norvegicus', 'r. norvegicus'],
        'danio rerio': ['zebrafish', 'danio rerio', 'd. rerio'],
        'drosophila melanogaster': ['fly', 'drosophila', 'd. melanogaster'],
    }

    text_lower = text.lower()
    for canonical, aliases in organisms.items():
        for alias in aliases:
            if alias in text_lower:
                return canonical

    return 'unknown'


def extract_tissue(text: str) -> str:
    """Extract tissue type from text."""
    tissues = [
        'brain', 'heart', 'liver', 'kidney', 'lung', 'spleen',
        'muscle', 'skin', 'blood', 'bone', 'pancreas', 'intestine',
        'stomach', 'colon', 'breast', 'prostate', 'ovary', 'testis',
        'thyroid', 'adrenal', 'pituitary', 'retina', 'cornea',
        'tumor', 'cancer', 'carcinoma', 'lymphoma', 'leukemia',
    ]

    text_lower = text.lower()
    for tissue in tissues:
        if tissue in text_lower:
            return tissue

    return 'unknown'


def extract_technology(text: str) -> str:
    """Extract sequencing technology from text."""
    technologies = {
        '10x Genomics': ['10x', '10x genomics', 'chromium'],
        'Visium': ['visium', 'spatial transcriptomics'],
        'Smart-seq': ['smart-seq', 'smartseq'],
        'Drop-seq': ['drop-seq', 'dropseq'],
        'MERFISH': ['merfish'],
        'seqFISH': ['seqfish'],
        'Slide-seq': ['slide-seq', 'slideseq'],
        'Xenium': ['xenium'],
    }

    text_lower = text.lower()
    for tech, aliases in technologies.items():
        for alias in aliases:
            if alias in text_lower:
                return tech

    return 'unknown'


def extract_metadata(text: str, benchmark_type: str = None) -> Dict[str, any]:
    """Extract all metadata from text, optionally filtered by benchmark type."""
    metadata = {
        'geo_accessions': extract_geo_accessions(text),
        'sra_accessions': extract_sra_accessions(text),
        'cellxgene_accessions': extract_cellxgene_accessions(text),
        'organism': extract_organism(text),
        'tissue': extract_tissue(text),
        'technology': extract_technology(text),
    }
    
    if benchmark_type:
        metadata = _filter_by_benchmark_type(metadata, benchmark_type, text)
    
    return metadata


def _filter_by_benchmark_type(metadata: Dict[str, any], benchmark_type: str, text: str) -> Dict[str, any]:
    """Filter and prioritize metadata based on benchmark type."""
    # Add relevance scores and filter based on benchmark type
    text_lower = text.lower()
    
    # Define keywords for each benchmark type
    benchmark_keywords = {
        'integration': ['integration', 'multi-omics', 'multiome', 'batch correction', 'harmony', 'seurat', 'scanorama'],
        'matching': ['matching', 'alignment', 'registration', 'correspondence'],
        'clustering': ['clustering', 'cluster', 'unsupervised', 'k-means', 'hierarchical'],
        'annotation': ['annotation', 'cell type', 'classification', 'supervised'],
        'denoising': ['denoising', 'noise', 'denoise', 'filter'],
        'imputation': ['imputation', 'missing', 'dropout', 'scimpute', 'magic'],
        'batch_correction': ['batch', 'correction', 'combat', 'limma'],
        'trajectory': ['trajectory', 'pseudotime', 'monocle', 'slingshot'],
        'celltype': ['cell type', 'annotation', 'classification'],
        'spatial': ['spatial', 'visium', 'merfish', 'seqfish', 'slide-seq', 'xenium'],
        'multiome': ['multiome', 'multi-omics', 'atac', 'rna', 'protein', 'cite-seq'],
    }
    
    keywords = benchmark_keywords.get(benchmark_type, [])
    relevance_score = sum(1 for kw in keywords if kw in text_lower)
    
    # For spatial/multiome, prioritize relevant technologies
    if benchmark_type == 'spatial':
        if metadata['technology'] not in ['Visium', 'MERFISH', 'seqFISH', 'Slide-seq', 'Xenium']:
            relevance_score -= 1
    elif benchmark_type == 'multiome':
        if 'multi' not in text_lower and 'cite-seq' not in text_lower:
            relevance_score -= 1
    
    metadata['benchmark_type'] = benchmark_type
    metadata['relevance_score'] = relevance_score
    
    return metadata
