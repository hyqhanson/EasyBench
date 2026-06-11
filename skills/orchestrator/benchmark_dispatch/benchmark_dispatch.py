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
import sys
from pathlib import Path
from typing import Dict, List

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
    
    # Step 2.5: Download candidate datasets for collected accessions
    if not args.no_download:
        print("\n📦 Downloading candidate datasets...")
        collected_data['download_results'] = download_collected_datasets(
            collected_data, output_dir / 'downloaded_data'
        )
        collected_data['downloaded_datasets'] = {
            'geo': [res['gse_id'] for res in collected_data['download_results'] if res.get('source') == 'geo'],
            'sra': [res['sra_id'] for res in collected_data['download_results'] if res.get('source') == 'sra'],
            'cellxgene': [res['cellxgene_id'] for res in collected_data['download_results'] if res.get('source') == 'cellxgene'],
        }

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
        # GEO / SRA / cellxgene
        collected_data['datasets']['geo'].extend(metadata.get('geo_accessions', {}).get('gse', []) or metadata.get('geo_accessions', {}).get('gse', []))
        collected_data['datasets']['sra'].extend(metadata.get('sra_accessions', []) or metadata.get('sra_accessions', []))
        collected_data['datasets']['cellxgene'].extend(metadata.get('cellxgene_accessions', []) or metadata.get('cellxgene_accessions', []))
        # New sources
        collected_data['datasets']['arxiv'].extend(metadata.get('arxiv_ids', []) or metadata.get('arxiv_ids', []))
        collected_data['datasets']['github'].extend(metadata.get('github_repos', []) or metadata.get('github_repos', []))
        collected_data['datasets']['zenodo'].extend(metadata.get('zenodo_records', []) or metadata.get('zenodo_records', []))
    
    # Remove duplicates
    for key in collected_data['datasets']:
        collected_data['datasets'][key] = list(set(collected_data['datasets'][key]))
    
    # Filter: keep only literature results that have at least one dataset accession
    before = len(collected_data['literature_results'])
    collected_data['literature_results'] = [
        r for r in collected_data['literature_results']
        if (
            r.get('metadata', {}).get('geo_accessions', {}).get('gse', [])
            or r.get('metadata', {}).get('sra_accessions', [])
            or r.get('metadata', {}).get('cellxgene_accessions', [])
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
    """Download candidate datasets for the collected accession IDs."""
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
    
    downloaded = collected_data.get('downloaded_datasets', {})
    plan['data_requirements'] = {
        'datasets_found': sum(len(v) for v in datasets.values()),
        'geo_datasets': len(datasets.get('geo', [])),
        'sra_datasets': len(datasets.get('sra', [])),
        'cellxgene_datasets': len(datasets.get('cellxgene', [])),
        'downloaded_geo': len(downloaded.get('geo', [])),
        'downloaded_sra': len(downloaded.get('sra', [])),
        'downloaded_cellxgene': len(downloaded.get('cellxgene', [])),
    }
    
    return plan


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
- **GEO downloaded**: {len(collected_data.get('downloaded_datasets', {}).get('geo', []))}
- **SRA downloaded**: {len(collected_data.get('downloaded_datasets', {}).get('sra', []))}
- **cellxgene downloaded**: {len(collected_data.get('downloaded_datasets', {}).get('cellxgene', []))}

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