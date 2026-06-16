#!/usr/bin/env python3
"""Benchmark Dispatch Skill — Entry point for benchmark-type driven workflows.

Accepts a benchmark type and orchestrates data collection, literature parsing,
and initial analysis setup.

Usage:
    python benchmark_dispatch.py --benchmark-type integration --output <dir>
    python benchmark_dispatch.py --benchmark-type spatial --query "human brain" --output <dir>
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

# Add parent directories to path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'skills'))

# Import after path setup
from literature.core.extractor import extract_metadata
from literature.core.search import fetch_pubmed_article, search_pubmed
from literature.core.downloader import (
    download_cellxgene_dataset,
    download_from_zenodo,
    download_geo_dataset,
    download_sra_dataset,
)
from orchestrator.reproduce_paper import run_reproduce


def main():
    parser = argparse.ArgumentParser(description='Benchmark-type driven workflow dispatcher')
    parser.add_argument('--benchmark-type', required=True,
                       help='Type of benchmark to run (e.g. integration, spatial, ...)')
    parser.add_argument('--query', help='Search query for relevant literature/data')
    parser.add_argument('--input', help='Specific input (URL, DOI, PDF) to process')
    parser.add_argument('--repo-url', help='Explicit repository URL for reproduction')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--no-download', action='store_true',
                       help='Skip data download, only collect metadata')
    parser.add_argument('--no-reproduce', action='store_true',
                       help='Skip the reproduce-paper step')
    parser.add_argument('--reproduce-no-clone', action='store_true',
                       help='Skip repository cloning during reproduction')
    parser.add_argument('--reproduce-no-install', action='store_true',
                       help='Skip environment installation during reproduction')
    parser.add_argument('--reproduce-no-run', action='store_true',
                       help='Skip execution during reproduction')
    parser.add_argument('--reproduce-clone-depth', type=int, default=1,
                       help='Git clone depth for reproduction checkout')
    parser.add_argument('--use-llm', action='store_true',
                       help='Use LLM for intelligent literature search and dataset extraction')
    parser.add_argument('--benchmark-data-dir',
                       default=str(Path(__file__).resolve().parent.parent.parent.parent / 'benchmark_data'),
                       help='Root directory for per-paper downloaded benchmark data '
                            '(default: OmicsClaw/benchmark_data/)')

    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"🚀 Starting {args.benchmark_type} benchmark workflow")
    print(f"Output directory: {output_dir}")

    # Step 1: Generate search queries based on benchmark type
    search_queries = generate_search_queries(args.benchmark_type, args.query)
    
    # Step 2: Collect literature and extract datasets
    collected_data = collect_benchmark_data(search_queries, args.benchmark_type, 
                                          args.input, output_dir, args.no_download,
                                          use_llm=args.use_llm)
    
    # Step 2.5: Save FULLY_ACCEPTED papers to per-benchmark, per-paper folder structure
    benchmark_data_dir = Path(args.benchmark_data_dir)
    accepted_summary = save_accepted_papers(
        collected_data,
        benchmark_data_dir,
        args.benchmark_type,
        download_data=not args.no_download,
    )
    collected_data['accepted_papers_summary'] = accepted_summary

    # Step 2.6: Re-discover data for papers whose initial downloads are insufficient
    if not args.no_download:
        rediscovery_result = rediscover_paper_data_if_needed(benchmark_data_dir, args.benchmark_type)
        collected_data['rediscovery'] = rediscovery_result

    if not args.no_reproduce:
        print("\n🔧 Running reproduce-paper workflow...")
        reproduce_input = args.input or args.repo_url or None
        if reproduce_input is None and collected_data['literature_results']:
            reproduce_input = collected_data['literature_results'][0].get('raw_text') or collected_data['literature_results'][0].get('input')

        if reproduce_input:
            reproduce_result = run_reproduce(
                reproduce_input,
                'auto',
                args.repo_url,
                args.benchmark_type,
                output_dir,
                no_clone=args.reproduce_no_clone,
                no_install=args.reproduce_no_install,
                no_run=args.reproduce_no_run,
                clone_depth=args.reproduce_clone_depth,
            )
            collected_data['reproduce'] = reproduce_result
        else:
            print('  No suitable text available to run reproduce-paper.')

    # Step 3: Generate workflow plan
    workflow_plan = generate_workflow_plan(args.benchmark_type, collected_data)
    
    # Step 4: Save results
    save_results(workflow_plan, collected_data, output_dir)
    
    print(f"\n✅ Benchmark setup complete!")
    print(f"📋 Workflow plan saved to: {output_dir / 'workflow_plan.json'}")
    print(f"📊 Collected data summary: {output_dir / 'data_summary.md'}")


def generate_search_queries(benchmark_type: str, user_query: str = None) -> List[str]:
    """Generate search queries for the benchmark type."""
    base_queries = {
        'integration': [
            f"{benchmark_type} single-cell RNA-seq datasets",
            f"multi-omics {benchmark_type} benchmark",
            f"batch effect correction {benchmark_type}",
        ],
        'spatial': [
            f"spatial transcriptomics {benchmark_type}",
            f"Visium {benchmark_type} analysis",
            f"spatial clustering benchmark",
        ],
        'multiome': [
            f"multiome {benchmark_type}",
            f"CITE-seq {benchmark_type}",
            f"ATAC + RNA {benchmark_type}",
        ],
        'clustering': [
            f"single-cell clustering benchmark",
            f"unsupervised clustering evaluation",
            f"cluster stability assessment",
        ],
        'annotation': [
            f"cell type annotation benchmark",
            f"supervised classification single-cell",
            f"reference-based annotation",
        ],
        'trajectory': [
            f"trajectory inference benchmark",
            f"pseudotime analysis evaluation",
            f"developmental trajectory",
        ],
        'batch_correction': [
            f"batch effect correction benchmark",
            f"dataset integration evaluation",
            f"batch normalization assessment",
        ],
    }
    
    queries = base_queries.get(benchmark_type, [f"{benchmark_type} benchmark"])
    if user_query:
        queries.insert(0, user_query)
    
    return queries


def collect_benchmark_data(search_queries: List[str], benchmark_type: str, 
                          specific_input: str, output_dir: Path, no_download: bool,
                          use_llm: bool = False) -> Dict:
    """Collect data using literature parsing skill."""
    collected_data = {
        'benchmark_type': benchmark_type,
        'search_queries': search_queries,
        'literature_results': [],
        'datasets': {'geo': [], 'sra': [], 'cellxgene': [], 'arxiv': [], 'github': [], 'zenodo': []},
        'total_relevance_score': 0,
    }
    
    literature_dir = output_dir / 'literature'
    literature_dir.mkdir(exist_ok=True)
    
    # If specific input provided, process it directly (LLM or hardcoded)
    if specific_input:
        print(f"\n📄 Processing specific input: {specific_input}")
        result = process_literature_input(specific_input, benchmark_type, 
                                        literature_dir / 'input_0', no_download)
        if result:
            collected_data['literature_results'].append(result)
            collected_data['total_relevance_score'] += result.get('relevance_score', 0)
    elif use_llm:
        # LLM-powered collection
        print(f"\n🤖 Using LLM-powered literature search for: {benchmark_type}")
        try:
            from literature.core.llm_collector import llm_collect_literature
            import time as _time
            _t0 = _time.time()
            llm_results = llm_collect_literature(
                benchmark_type,
                user_query=search_queries[0] if search_queries else None,
            )
            # Save full LLM results list and audit trail
            (literature_dir / 'llm_results.json').write_text(json.dumps(llm_results, indent=2))
            if isinstance(llm_results, dict) and 'audit' in llm_results:
                (literature_dir / 'llm_audit.json').write_text(json.dumps(llm_results['audit'], indent=2))
            results_list = llm_results.get('results', llm_results) if isinstance(llm_results, dict) else llm_results
            _elapsed = _time.time() - _t0
            print(f"\n  📊 LLM search complete: {len(results_list)} papers in {_elapsed:.0f}s")
            for result in results_list:
                # Identifier may be pmid/arxiv id/github id etc.
                identifier = result.get('pmid') or result.get('id') or result.get('input', '')[:32]
                title = result.get('input', result.get('title', ''))[:120]
                print(f"    LLM found: {title} ({identifier})")
                # Save each result to its own folder
                safe_name = f"llm_{str(identifier).replace('/', '_')[:80]}"
                out = literature_dir / safe_name
                out.mkdir(exist_ok=True)
                (out / 'extracted_metadata.json').write_text(
                    json.dumps(result, indent=2)
                )
                collected_data['literature_results'].append(result)
                collected_data['total_relevance_score'] += result.get('relevance_score', 0) or result.get('metadata', {}).get('benchmark_relevance_score', 0)
            # Print acceptance summary
            _acc = {}
            for r in results_list:
                _acc[r.get('acceptance', 'UNKNOWN')] = _acc.get(r.get('acceptance', 'UNKNOWN'), 0) + 1
            _acc_str = ' · '.join(f'{k}={v}' for k, v in sorted(_acc.items()))
            print(f"  📊 Acceptance: {_acc_str}")
        except Exception as e:
            print(f"  LLM collection failed ({e}), falling back to hardcoded search.")
            use_llm = False  # fallback to hardcoded below

    if not specific_input and not use_llm:
        # Hardcoded PubMed search (existing logic)
        print(f"\n🔍 Performing PubMed search for: {benchmark_type}")
        seen_pmids = set()
        for query in search_queries[:3]:
            print(f"  Query: {query}")
            pmids = search_pubmed(query, max_results=3)
            for pmid in pmids:
                if pmid in seen_pmids:
                    continue
                seen_pmids.add(pmid)
                article = fetch_pubmed_article(pmid)
                if not article:
                    continue
                print(f"    Found PubMed article: {article.get('title', '')[:80]} ({pmid})")
                result = process_pubmed_article(article, benchmark_type,
                                               literature_dir / f'pubmed_{pmid}', no_download)
                if result:
                    collected_data['literature_results'].append(result)
                    collected_data['total_relevance_score'] += result.get('relevance_score', 0)
        
        if not collected_data['literature_results']:
            print("  No PubMed results found; falling back to mock search examples.")
            for i, query in enumerate(search_queries[:2]):
                mock_text = f"This paper presents a {benchmark_type} benchmark using GSE{i+1}23456 and SRP{i+1}654321 datasets."
                result = process_mock_literature(mock_text, benchmark_type,
                                               literature_dir / f'search_{i}', no_download)
                if result:
                    collected_data['literature_results'].append(result)
                    collected_data['total_relevance_score'] += result.get('relevance_score', 0)
    
    # Aggregate datasets
    for result in collected_data['literature_results']:
        metadata = result.get('metadata', {})
        # LLM collector outputs top-level keys (gse_ids, sra_ids, cellxgene_ids)
        # Hardcoded/pubmed results use nested metadata.geo_accessions.gse format
        llm_gse = result.get('gse_ids', [])
        llm_sra = result.get('sra_ids', [])
        llm_cellx = result.get('cellxgene_ids', [])
        llm_github = result.get('github_repos', [])
        # GEO / SRA / cellxgene
        collected_data['datasets']['geo'].extend(
            metadata.get('geo_accessions', {}).get('gse', []) or llm_gse
        )
        collected_data['datasets']['sra'].extend(
            metadata.get('sra_accessions', []) or llm_sra
        )
        collected_data['datasets']['cellxgene'].extend(
            metadata.get('cellxgene_accessions', []) or llm_cellx
        )
        # New sources
        collected_data['datasets']['arxiv'].extend(
            metadata.get('arxiv_ids', []) or result.get('arxiv_ids', [])
        )
        collected_data['datasets']['github'].extend(
            metadata.get('github_repos', []) or llm_github
        )
        collected_data['datasets']['zenodo'].extend(
            metadata.get('zenodo_records', []) or
            (result.get('zenodo_data', []) + result.get('zenodo_code', []))
        )
    
    # Remove duplicates
    for key in collected_data['datasets']:
        collected_data['datasets'][key] = list(set(collected_data['datasets'][key]))
    
    # Filter: keep only literature results that have at least one dataset accession
    before = len(collected_data['literature_results'])
    collected_data['literature_results'] = [
        r for r in collected_data['literature_results']
        if (
            # Old format: nested under metadata.geo_accessions.gse
            r.get('metadata', {}).get('geo_accessions', {}).get('gse', [])
            or r.get('metadata', {}).get('sra_accessions', [])
            or r.get('metadata', {}).get('cellxgene_accessions', [])
            # LLM collector format: top-level keys
            or r.get('gse_ids', [])
            or r.get('sra_ids', [])
            or r.get('cellxgene_ids', [])
        )
    ]
    after = len(collected_data['literature_results'])
    if before != after:
        print(f"  Filtered out {before - after} literature result(s) with no dataset accessions.")
    
    return collected_data


def process_literature_input(input_text: str, benchmark_type: str, 
                           output_dir: Path, no_download: bool) -> Dict:
    """Process a specific literature input."""
    try:
        # Use direct function call instead of subprocess
        from literature.core.parser import parse_input
        from literature.core.extractor import extract_metadata
        from literature.core.steps import extract_paper_steps
        
        text, detected_type = parse_input(input_text, 'auto')
        if not text or text.startswith('Error'):
            return None
            
        metadata = extract_metadata(text, benchmark_type)
        paper_steps = extract_paper_steps(text)
        metadata['paper_steps'] = paper_steps
        
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Save metadata
        metadata_file = output_dir / 'extracted_metadata.json'
        metadata_file.write_text(json.dumps(metadata, indent=2))

        # Generate simple report
        report_file = output_dir / 'report.md'
        report = f"""# Literature Parsing Report

**Input**: {input_text}
**Type**: {detected_type}
**Benchmark Type**: {benchmark_type}

## Metadata
- Organism: {metadata.get('organism', 'unknown')}
- Tissue: {metadata.get('tissue', 'unknown')}
- Technology: {metadata.get('technology', 'unknown')}
- Relevance Score: {metadata.get('relevance_score', 0)}

## Datasets
- GEO: {metadata.get('geo_accessions', {}).get('gse', [])}
- SRA: {metadata.get('sra_accessions', [])}
- cellxgene: {metadata.get('cellxgene_accessions', [])}
"""
        report_file.write_text(report)
        
        return {
            'input': input_text,
            'raw_text': text,
            'metadata': metadata,
            'paper_steps': paper_steps,
            'relevance_score': metadata.get('relevance_score', 0),
            'output_dir': str(output_dir),
        }
    except Exception as e:
        print(f"Error processing input: {e}")
        import traceback
        traceback.print_exc()
    
    return None


def download_collected_datasets(collected_data: Dict, output_dir: Path) -> List[Dict]:
    """Download candidate datasets for the collected accession IDs.

    .. deprecated::
       Use ``save_accepted_papers()`` instead — it organises downloads
       per paper under ``benchmark_data/{benchmark_type}/{paper_slug}/data/``.
    """
    download_results = []
    output_dir.mkdir(parents=True, exist_ok=True)

    geo_ids = collected_data.get('datasets', {}).get('geo', [])
    sra_ids = collected_data.get('datasets', {}).get('sra', [])
    cellxgene_ids = collected_data.get('datasets', {}).get('cellxgene', [])

    for gse_id in geo_ids:
        print(f"    Downloading GEO dataset: {gse_id}")
        download_results.append(download_geo_dataset(gse_id, output_dir / 'geo'))

    for sra_id in sra_ids:
        print(f"    Downloading SRA dataset: {sra_id}")
        download_results.append(download_sra_dataset(sra_id, output_dir / 'sra'))

    for dataset_id in cellxgene_ids:
        print(f"    Downloading cellxgene dataset: {dataset_id}")
        download_results.append(download_cellxgene_dataset(dataset_id, output_dir / 'cellxgene'))

    return download_results


def process_pubmed_article(article: Dict[str, str], benchmark_type: str,
                            output_dir: Path, no_download: bool) -> Dict:
    """Process a PubMed article and extract dataset metadata."""
    try:
        from literature.core.steps import extract_paper_steps

        text = ' '.join([article.get('title', ''), article.get('abstract', '')])
        metadata = extract_metadata(text, benchmark_type)
        paper_steps = extract_paper_steps(text)
        output_dir.mkdir(parents=True, exist_ok=True)

        metadata_file = output_dir / 'extracted_metadata.json'
        metadata_file.write_text(json.dumps({
            'article': article,
            'metadata': metadata,
            'paper_steps': paper_steps,
        }, indent=2))

        report_file = output_dir / 'report.md'
        report = f"""# PubMed Literature Parsing Report

**PMID**: {article.get('pmid')}
**Title**: {article.get('title')}
**Journal**: {article.get('journal')}
**Authors**: {article.get('authors')}
**Benchmark Type**: {benchmark_type}

## Metadata
- Organism: {metadata.get('organism', 'unknown')}
- Tissue: {metadata.get('tissue', 'unknown')}
- Technology: {metadata.get('technology', 'unknown')}
- Relevance Score: {metadata.get('relevance_score', 0)}

## Datasets
- GEO: {metadata.get('geo_accessions', {}).get('gse', [])}
- SRA: {metadata.get('sra_accessions', [])}
- cellxgene: {metadata.get('cellxgene_accessions', [])}
"""
        report_file.write_text(report)

        return {
            'input': article.get('title', ''),
            'pmid': article.get('pmid'),
            'raw_text': text,
            'metadata': metadata,
            'paper_steps': paper_steps,
            'relevance_score': metadata.get('relevance_score', 0),
            'output_dir': str(output_dir),
        }
    except Exception as e:
        print(f"Error processing PubMed article: {e}")
    
    return None


def process_mock_literature(text: str, benchmark_type: str, 
                          output_dir: Path, no_download: bool) -> Dict:
    """Process mock literature text."""
    try:
        from literature.core.steps import extract_paper_steps
        metadata = extract_metadata(text, benchmark_type)
        paper_steps = extract_paper_steps(text)
        output_dir.mkdir(exist_ok=True)
        
        # Save metadata
        metadata_file = output_dir / 'extracted_metadata.json'
        metadata_file.write_text(json.dumps({
            'metadata': metadata,
            'paper_steps': paper_steps,
        }, indent=2))
        
        return {
            'input': text,
            'raw_text': text,
            'metadata': metadata,
            'paper_steps': paper_steps,
            'relevance_score': metadata.get('relevance_score', 0),
            'output_dir': str(output_dir),
        }
    except Exception as e:
        print(f"Error processing mock literature: {e}")
    
    return None


def generate_workflow_plan(benchmark_type: str, collected_data: Dict) -> Dict:
    """Generate a workflow plan based on benchmark type and collected data."""
    plan = {
        'benchmark_type': benchmark_type,
        'stages': [],
        'required_skills': [],
        'estimated_time': '2-4 hours',
        'data_requirements': {},
    }
    
    # Define workflow stages for each benchmark type
    workflows = {
        'integration': [
            {'stage': 'data_collection', 'description': 'Collect multi-omics datasets'},
            {'stage': 'preprocessing', 'description': 'Normalize and filter data'},
            {'stage': 'integration', 'description': 'Apply integration methods (Seurat, Harmony, etc.)'},
            {'stage': 'evaluation', 'description': 'Assess integration quality'},
        ],
        'spatial': [
            {'stage': 'data_collection', 'description': 'Collect spatial transcriptomics data'},
            {'stage': 'preprocessing', 'description': 'Spatial preprocessing and quality control'},
            {'stage': 'domain_detection', 'description': 'Identify tissue domains'},
            {'stage': 'evaluation', 'description': 'Spatial analysis evaluation'},
        ],
        'clustering': [
            {'stage': 'data_collection', 'description': 'Collect single-cell datasets'},
            {'stage': 'preprocessing', 'description': 'QC and normalization'},
            {'stage': 'clustering', 'description': 'Apply clustering algorithms'},
            {'stage': 'evaluation', 'description': 'Assess cluster quality and stability'},
        ],
    }
    
    plan['stages'] = workflows.get(benchmark_type, [
        {'stage': 'data_collection', 'description': 'Collect relevant datasets'},
        {'stage': 'analysis', 'description': f'Perform {benchmark_type} analysis'},
        {'stage': 'evaluation', 'description': f'Evaluate {benchmark_type} results'},
    ])
    
    # Determine required skills based on data types
    datasets = collected_data.get('datasets', {})
    if datasets.get('geo') or datasets.get('sra'):
        plan['required_skills'].append('literature')
    if any(datasets.values()):
        plan['required_skills'].append('data-download')
    
    # Add benchmark-specific skills
    if benchmark_type == 'integration':
        plan['required_skills'].extend(['sc-preprocessing', 'integration'])
    elif benchmark_type == 'spatial':
        plan['required_skills'].extend(['spatial-preprocess', 'spatial-domains'])
    elif benchmark_type == 'clustering':
        plan['required_skills'].extend(['sc-preprocessing', 'clustering'])
    
    downloaded = collected_data.get('accepted_papers_summary', {})
    plan['data_requirements'] = {
        'datasets_found': sum(len(v) for v in datasets.values()),
        'geo_datasets': len(datasets.get('geo', [])),
        'sra_datasets': len(datasets.get('sra', [])),
        'cellxgene_datasets': len(datasets.get('cellxgene', [])),
        'accepted_papers_saved': downloaded.get('saved_count', 0),
    }
    
    return plan


def _sanitize_folder_name(name: str, max_len: int = 60) -> str:
    """Convert a paper title or identifier into a safe folder name."""
    # Remove or replace unsafe characters
    safe = re.sub(r'[<>:"/\\|?*]', '', name)
    # Replace whitespace and common separators with hyphens
    safe = re.sub(r'[\s,;:!]+', '-', safe)
    # Collapse multiple hyphens
    safe = re.sub(r'-{2,}', '-', safe)
    # Strip leading/trailing hyphens and dots
    safe = safe.strip('-.').lower()
    # Truncate
    if len(safe) > max_len:
        # Try to cut at a hyphen boundary
        cutoff = safe.rfind('-', 0, max_len)
        if cutoff > max_len // 2:
            safe = safe[:cutoff]
        else:
            safe = safe[:max_len]
    return safe or 'paper'


def save_accepted_papers(
    collected_data: Dict[str, Any],
    root_dir: Path,
    benchmark_type: str,
    download_data: bool = True,
) -> Dict[str, Any]:
    """Save FULLY_ACCEPTED papers to a per-benchmark, per-paper folder hierarchy.

    Creates::

        {root_dir}/
          {benchmark_type}/
            {paper_slug}/
              paper_metadata.json
              data/
                {gse_id}/
                {sra_id}/
                ...
    """
    literature_results = collected_data.get('literature_results', [])
    accepted = [r for r in literature_results if r.get('acceptance') == 'FULLY_ACCEPTED']

    if not accepted:
        print('  ⚠️  No FULLY_ACCEPTED papers to save.')
        return {'saved': 0, 'benchmark_type': benchmark_type, 'papers': []}

    bench_dir = root_dir / benchmark_type
    bench_dir.mkdir(parents=True, exist_ok=True)

    print(f'\n  📁 Saving {len(accepted)} FULLY_ACCEPTED paper(s) to: {bench_dir}')

    summary: Dict[str, Any] = {
        'benchmark_type': benchmark_type,
        'saved_count': 0,
        'papers': [],
    }

    for idx, paper in enumerate(accepted):
        title = paper.get('title', '')
        doi = paper.get('doi', '')
        doi_suffix = doi.rsplit('/', 1)[-1] if doi and '/' in doi else ''

        # Build a human-readable folder slug from title + DOI
        if title:
            # Take first 8 meaningful words
            words = [w for w in title.lower().split() if len(w) > 2][:8]
            slug = _sanitize_folder_name('-'.join(words), max_len=60)
        else:
            slug = doi_suffix or f'paper_{idx + 1}'
        if doi_suffix and doi_suffix not in slug:
            slug = f'{slug}-{_sanitize_folder_name(doi_suffix, max_len=20)}'

        paper_dir = bench_dir / slug
        # Handle duplicate slugs
        if paper_dir.exists():
            paper_dir = bench_dir / f'{slug}-{idx}'
        paper_dir.mkdir(parents=True, exist_ok=True)

        # ── Build paper_metadata.json ──
        metadata = {
            'title': title,
            'doi': doi,
            'acceptance': paper.get('acceptance'),
            'source': paper.get('source'),
            'gse_ids': paper.get('gse_ids', []),
            'sra_ids': paper.get('sra_ids', []),
            'cellxgene_ids': paper.get('cellxgene_ids', []),
            'github_repos': paper.get('github_repos', []),
            'zenodo_data': paper.get('zenodo_data', []),
            'zenodo_code': paper.get('zenodo_code', []),
            'figshare_links': paper.get('figshare_links', []),
            'organism': paper.get('organism', ''),
            'tissue': paper.get('tissue', ''),
            'technology': paper.get('technology', ''),
            'relevance_score': paper.get('relevance_score', 0),
            'methods_summary': paper.get('methods_summary', ''),
            'reason': paper.get('reason', ''),
            'benchmark_type': benchmark_type,
            'saved_at': None,  # filled after download
        }

        # ── Download data for this paper ──
        data_dir = paper_dir / 'data'
        data_dir.mkdir(exist_ok=True)

        download_results: List[Dict[str, Any]] = []

        # Filter out INFERRED_DATA placeholders
        gse_ids = [g for g in paper.get('gse_ids', []) if g and g != 'INFERRED_DATA']
        sra_ids = paper.get('sra_ids', []) or []
        cellxgene_ids = paper.get('cellxgene_ids', []) or []

        if download_data:

            for gse_id in gse_ids:
                print(f'    📥 [{slug}] Downloading GEO: {gse_id}')
                try:
                    result = download_geo_dataset(gse_id, data_dir / gse_id)
                    download_results.append(result)
                    metadata.setdefault('downloaded_data', []).append({
                        'type': 'geo',
                        'id': gse_id,
                        'status': result.get('status'),
                        'path': str(data_dir / gse_id),
                    })
                except Exception as exc:
                    print(f'    ⚠️  Failed to download GEO {gse_id}: {exc}')

            for sra_id in sra_ids:
                print(f'    📥 [{slug}] Downloading SRA: {sra_id}')
                try:
                    result = download_sra_dataset(sra_id, data_dir / sra_id)
                    download_results.append(result)
                    metadata.setdefault('downloaded_data', []).append({
                        'type': 'sra',
                        'id': sra_id,
                        'status': result.get('status'),
                        'path': str(data_dir / sra_id),
                    })
                except Exception as exc:
                    print(f'    ⚠️  Failed to download SRA {sra_id}: {exc}')

            # Zenodo data (processed data)
            zenodo_links = paper.get('zenodo_data', []) or []
            for zenodo_url in zenodo_links:
                print(f'    📥 [{slug}] Downloading Zenodo data: {zenodo_url}')
                try:
                    result = download_from_zenodo(zenodo_url, data_dir / f'zenodo_{_sanitize_folder_name(zenodo_url[-20:], max_len=30)}')
                    download_results.append(result)
                    metadata.setdefault('downloaded_data', []).append({
                        'type': 'zenodo_data',
                        'id': zenodo_url,
                        'status': result.get('status'),
                        'path': str(data_dir),
                    })
                except Exception as exc:
                    print(f'    ⚠️  Failed to download Zenodo data {zenodo_url}: {exc}')

            # Zenodo code (GitHub snapshots — useful for re-discovery)
            zenodo_code_links = paper.get('zenodo_code', []) or []
            for zenodo_url in zenodo_code_links:
                print(f'    📥 [{slug}] Downloading Zenodo code: {zenodo_url}')
                try:
                    result = download_from_zenodo(zenodo_url, data_dir / f'zenodo_{_sanitize_folder_name(zenodo_url[-20:], max_len=30)}')
                    download_results.append(result)
                    metadata.setdefault('downloaded_data', []).append({
                        'type': 'zenodo_code',
                        'id': zenodo_url,
                        'status': result.get('status'),
                        'path': str(data_dir),
                    })
                except Exception as exc:
                    print(f'    ⚠️  Failed to download Zenodo code {zenodo_url}: {exc}')

            for cx_id in cellxgene_ids:
                print(f'    📥 [{slug}] Downloading cellxgene: {cx_id}')
                try:
                    result = download_cellxgene_dataset(cx_id, data_dir / cx_id.replace('/', '_'))
                    download_results.append(result)
                    metadata.setdefault('downloaded_data', []).append({
                        'type': 'cellxgene',
                        'id': cx_id,
                        'status': result.get('status'),
                        'path': str(data_dir / cx_id.replace('/', '_')),
                    })
                except Exception as exc:
                    print(f'    ⚠️  Failed to download cellxgene {cx_id}: {exc}')

        # ── Fill timestamp and write metadata ──
        from datetime import datetime, timezone
        metadata['saved_at'] = datetime.now(timezone.utc).isoformat()

        meta_path = paper_dir / 'paper_metadata.json'
        meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding='utf-8')

        paper_info = {
            'slug': slug,
            'title': title[:120],
            'path': str(paper_dir),
            'metadata_file': str(meta_path),
            'gse_ids': gse_ids,
            'sra_ids': sra_ids,
            'cellxgene_ids': cellxgene_ids,
            'github_repos': paper.get('github_repos', []),
            'downloads': [dr.get('status') for dr in download_results],
        }
        summary['papers'].append(paper_info)
        print(f'    ✅ [{slug}]: metadata + {len(download_results)} download(s)')

    summary['saved_count'] = len(summary['papers'])

    # Write overall summary for this benchmark type
    summary_path = bench_dir / '_summary.json'
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'  📊 Summary saved to: {summary_path}')

    return summary


def rediscover_paper_data_if_needed(benchmark_data_dir: Path, benchmark_type: str) -> Dict[str, Any]:
    """After initial downloads, check if any FULLY_ACCEPTED paper lacks sc-data.

    For papers whose downloaded files contain no actual single-cell data
    (e.g. only PDFs, metadata JSON), this function scans the paper's
    GitHub repository for data links (Zenodo records, GEO IDs, download
    URLs in README/DATA.md) and attempts to download the real data.

    This implements the "agent-driven data re-discovery" pattern: if the
    links extracted by the LLM don't provide usable data, the system
    autonomously searches the paper's own code repository for clues.
    """
    bench_dir = benchmark_data_dir / benchmark_type
    if not bench_dir.exists():
        return {'rediscovered': 0, 'papers': []}

    import zipfile as _zipfile

    # File extensions that indicate actual single-cell data
    _DATA_EXTENSIONS = {'.h5ad', '.h5', '.mtx', '.loom', '.rds', '.h5seurat'}

    def _zip_contains_data(zip_path: Path) -> bool:
        """Quick check: does a zip file contain sc-data files (not just code)?"""
        try:
            with _zipfile.ZipFile(zip_path) as zf:
                sample = zf.namelist()[:200]  # Check first 200 files
                data_count = sum(1 for n in sample
                               if Path(n).suffix.lower() in _DATA_EXTENSIONS)
                # If > 5% of sampled files are data files, it's a data zip
                return data_count > max(1, len(sample) * 0.05)
        except Exception:
            return False  # Can't read, assume not data

    results = []
    for paper_dir in sorted(bench_dir.iterdir()):
        if not paper_dir.is_dir():
            continue

        meta_path = paper_dir / 'paper_metadata.json'
        if not meta_path.exists():
            continue

        metadata = json.loads(meta_path.read_text(encoding='utf-8'))
        data_dir = paper_dir / 'data'
        if not data_dir.exists():
            continue

        # ── Check if paper already has real sc-data files ──
        has_data = False
        all_files = list(data_dir.rglob('*'))
        for f in all_files:
            if not f.is_file():
                continue
            if f.stat().st_size < 1024:
                continue
            # Direct sc-data files
            if f.suffix.lower() in _DATA_EXTENSIONS:
                has_data = True
                break
            # Zip files: only count as data if they contain sc-data inside
            if f.suffix.lower() == '.zip' and f.stat().st_size > 10 * 1024 * 1024:
                if _zip_contains_data(f):
                    has_data = True
                    break

        if has_data:
            continue  # Already has data, skip

        print(f'\n  🔍 [{paper_dir.name}] No sc-data found — re-discovering...')

        new_downloads = []

        # ── Strategy 1: Scan downloaded GitHub repo zips ──
        for f in all_files:
            if not f.is_file():
                continue
            if f.suffix.lower() != '.zip':
                continue
            if f.stat().st_size < 1024:
                continue

            new_urls = _extract_data_urls_from_zip(f)
            if new_urls:
                print(f'    Found {len(new_urls)} data URL(s) in {f.name}')

                # Try Zenodo records first
                zenodo_ids = set()
                for url in new_urls:
                    m = re.search(r'zenodo[./]records?/(\d+)', url)
                    if m:
                        zenodo_ids.add(m.group(1))
                    m = re.search(r'zenodo[./](\d{8})', url)
                    if m:
                        zenodo_ids.add(m.group(1))

                for zid in sorted(zenodo_ids):
                    print(f'    📥 Re-discovered Zenodo: {zid}')
                    try:
                        result = download_from_zenodo(zid, data_dir / f'zenodo_{zid}')
                        new_downloads.append({
                            'type': 'zenodo_rediscovered',
                            'id': zid,
                            'status': result.get('status'),
                            'path': str(data_dir / f'zenodo_{zid}'),
                        })
                    except Exception as exc:
                        print(f'    ⚠️  Failed: {exc}')

        # ── Strategy 2: Check paper's methods_summary / reason for hints ──
        # (Future: use LLM to extract implicit data links from text)

        # ── Update metadata ──
        if new_downloads:
            existing = metadata.get('downloaded_data', [])
            existing.extend(new_downloads)
            metadata['downloaded_data'] = existing
            metadata['_rediscovered_at'] = __import__('datetime').datetime.now(
                __import__('datetime').timezone.utc
            ).isoformat()
            meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding='utf-8')
            print(f'    ✅ Added {len(new_downloads)} re-discovered download(s)')

        results.append({
            'paper': paper_dir.name,
            'rediscovered': len(new_downloads),
        })

    total = sum(r['rediscovered'] for r in results)
    if total:
        print(f'\n  🔄 Data re-discovery complete: {total} new download(s) across {len(results)} paper(s)')
    return {'rediscovered': total, 'papers': results}


def _extract_data_urls_from_zip(zip_path: Path) -> List[str]:
    """Scan a GitHub repo zip for data download URLs in README/DATA files."""
    import zipfile as _zipfile
    urls = []
    try:
        with _zipfile.ZipFile(zip_path) as zf:
            # Look for README, DATA.md, and Python scripts in priority order
            candidates = [n for n in zf.namelist()
                         if any(k in n.lower() for k in ['readme', 'data.md', 'download', 'setup'])]
            for name in candidates[:5]:
                try:
                    text = zf.read(name).decode('utf-8', errors='replace')
                except Exception:
                    continue
                # Extract URLs
                found = re.findall(r'https?://[^\s\)\]\"\>]+', text)
                for u in found:
                    if any(k in u.lower() for k in [
                        'zenodo.org/record', 'zenodo.org/api',
                        'doi.org/10.5281/zenodo',
                        'figshare.com', 'ncbi.nlm.nih.gov/geo',
                        'cellxgene.cziscience.com',
                    ]):
                        urls.append(u.rstrip('.,;:'))
    except Exception as e:
        print(f'    ⚠️  Error reading zip {zip_path.name}: {e}')
    return list(set(urls))  # deduplicate


def save_results(workflow_plan: Dict, collected_data: Dict, output_dir: Path):
    """Save workflow plan and data summary."""
    # Save workflow plan
    plan_file = output_dir / 'workflow_plan.json'
    plan_file.write_text(json.dumps(workflow_plan, indent=2))
    
    # Generate data summary markdown
    summary_file = output_dir / 'data_summary.md'
    summary = f"""# {collected_data['benchmark_type'].title()} Benchmark Data Summary

**Generated**: {Path(__file__).stat().st_mtime}

## Overview
- **Benchmark Type**: {collected_data['benchmark_type']}
- **Total Relevance Score**: {collected_data['total_relevance_score']}
- **Literature Sources**: {len(collected_data['literature_results'])}

## Collected Datasets
- **GEO Datasets**: {len(collected_data['datasets']['geo'])}
  - {', '.join(collected_data['datasets']['geo'])}
- **SRA Datasets**: {len(collected_data['datasets']['sra'])}
  - {', '.join(collected_data['datasets']['sra'])}
- **cellxgene Datasets**: {len(collected_data['datasets']['cellxgene'])}
  - {', '.join(collected_data['datasets']['cellxgene'])}

## Downloaded Candidate Data
- **Accepted papers saved**: {collected_data.get('accepted_papers_summary', {}).get('saved_count', 0)}

## Workflow Plan
"""
    
    for stage in workflow_plan['stages']:
        summary += f"- **{stage['stage'].title()}**: {stage['description']}\n"
    
    summary += f"\n## Required Skills\n"
    for skill in workflow_plan['required_skills']:
        summary += f"- {skill}\n"

    reproduce_info = collected_data.get('reproduce')
    if reproduce_info:
        summary += "\n## Reproducibility Workflow\n"
        summary += f"- **Repository URL**: {reproduce_info.get('repo_url', 'N/A')}\n"
        summary += f"- **Reproduction Status**: {reproduce_info.get('status', 'unknown')}\n"
        statuses = reproduce_info.get('statuses', {})
        summary += f"- **Clone Success**: {statuses.get('clone_success', False)}\n"
        summary += f"- **Install Success**: {statuses.get('install_success', False)}\n"
        summary += f"- **Run Success**: {statuses.get('run_success', False)}\n"
        if reproduce_info.get('plan_path'):
            summary += f"- **Plan File**: {reproduce_info['plan_path']}\n"
        if reproduce_info.get('result_path'):
            summary += f"- **Result File**: {reproduce_info['result_path']}\n"

    summary_file.write_text(summary)


if __name__ == '__main__':
    main()