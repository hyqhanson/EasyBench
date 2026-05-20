#!/usr/bin/env python3
"""Literature parsing CLI - extract dataset accessions from papers and download sources."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from skills.literature.core.parser import parse_input
from skills.literature.core.extractor import extract_metadata
from skills.literature.core.downloader import (
    download_cellxgene_dataset,
    download_geo_dataset,
    download_sra_dataset,
)


def main():
    parser = argparse.ArgumentParser(description='Parse literature and extract dataset accessions')
    parser.add_argument('--input', required=True, help='URL, DOI, PubMed ID, PDF path, or text')
    parser.add_argument('--input-type', default='auto',
                       choices=['auto', 'url', 'doi', 'pubmed', 'file', 'text'],
                       help='Input type (default: auto-detect)')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--no-download', action='store_true',
                       help='Extract metadata only, skip download')
    parser.add_argument('--data-dir', help='Data directory for downloads (default: data/)')
    parser.add_argument('--benchmark-type', 
                       choices=['integration', 'matching', 'clustering', 'annotation', 'denoising', 'imputation', 'batch_correction', 'trajectory', 'celltype', 'spatial', 'multiome'],
                       help='Benchmark type to prioritize relevant datasets')

    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        project_root = Path(__file__).parent.parent.parent
        data_dir = project_root / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"Parsing input: {args.input}")

    text, detected_type = parse_input(args.input, args.input_type)
    print(f"Detected input type: {detected_type}")

    if not text or text.startswith('Error'):
        print(f"Failed to parse input: {text}")
        sys.exit(1)

    print("Extracting metadata...")
    metadata = extract_metadata(text, benchmark_type=args.benchmark_type)

    geo_acc = metadata.get('geo_accessions', {})
    sra_ids = metadata.get('sra_accessions', [])
    cellxgene_ids = metadata.get('cellxgene_accessions', [])

    print(f"Found {len(geo_acc.get('gse', []))} GEO datasets: {', '.join(geo_acc.get('gse', []))}")
    print(f"Found {len(sra_ids)} SRA accessions: {', '.join(sra_ids)}")
    print(f"Found {len(cellxgene_ids)} cellxgene datasets: {', '.join(cellxgene_ids)}")
    print(f"Organism: {metadata['organism']}")
    print(f"Tissue: {metadata['tissue']}")
    print(f"Technology: {metadata['technology']}")
    if args.benchmark_type:
        print(f"Benchmark type: {args.benchmark_type}")
        print(f"Relevance score: {metadata.get('relevance_score', 0)}")

    metadata_file = output_dir / 'extracted_metadata.json'
    metadata_file.write_text(json.dumps(metadata, indent=2))

    download_results = []
    if not args.no_download:
        if geo_acc.get('gse'):
            print(f"\nDownloading GEO datasets to {data_dir}...")
            for gse_id in geo_acc.get('gse', []):
                print(f"\nProcessing {gse_id}...")
                result = download_geo_dataset(gse_id, data_dir)
                download_results.append(result)
                print_download_status(result, gse_id)

        if sra_ids:
            print(f"\nDownloading SRA metadata to {data_dir}...")
            for sra_id in sra_ids:
                print(f"\nProcessing {sra_id}...")
                result = download_sra_dataset(sra_id, data_dir)
                download_results.append(result)
                print_download_status(result, sra_id)

        if cellxgene_ids:
            print(f"\nDownloading cellxgene candidates to {data_dir}...")
            for dataset_id in cellxgene_ids:
                print(f"\nProcessing {dataset_id}...")
                result = download_cellxgene_dataset(dataset_id, data_dir)
                download_results.append(result)
                print_download_status(result, dataset_id)

    generate_report(output_dir, metadata, download_results, args.no_download)

    print(f"\n✓ Results saved to {output_dir}")
    print(f"  - Report: {output_dir / 'report.md'}")
    print(f"  - Metadata: {metadata_file}")

    if download_results and not args.no_download:
        print(f"  - Downloaded data: {data_dir}")


def print_download_status(result: dict, accession: str) -> None:
    if result['status'] == 'success':
        print(f"✓ {accession}: Downloaded {len(result['files'])} files")
    elif result['status'] == 'partial':
        print(f"⚠ {accession}: Partial download ({len(result['files'])} files)")
    else:
        print(f"✗ {accession}: Failed - {', '.join(result.get('errors', []))}")


def generate_report(output_dir: Path, metadata: dict, download_results: list, no_download: bool):
    """Generate markdown report."""
    report_lines = [
        "# Literature Parsing Report",
        f"\n**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "\n## Extracted Metadata",
        f"\n- **Organism**: {metadata['organism']}",
        f"- **Tissue**: {metadata['tissue']}",
        f"- **Technology**: {metadata['technology']}",
    ]

    geo_acc = metadata.get('geo_accessions', {})
    if geo_acc.get('gse'):
        report_lines.append("\n## GEO Datasets\n")
        for gse_id in geo_acc['gse']:
            report_lines.append(f"- **{gse_id}**")

    if metadata.get('sra_accessions'):
        report_lines.append("\n## SRA Accessions\n")
        for sra_id in metadata['sra_accessions']:
            report_lines.append(f"- **{sra_id}**")

    if metadata.get('cellxgene_accessions'):
        report_lines.append("\n## cellxgene Datasets\n")
        for dataset_id in metadata['cellxgene_accessions']:
            report_lines.append(f"- **{dataset_id}**")

    if download_results:
        report_lines.append("\n## Download Results\n")
        for result in download_results:
            accession = result.get('gse_id') or result.get('sra_id') or result.get('cellxgene_id')
            source = result.get('source', 'unknown')
            status = result['status']
            files = result['files']
            report_lines.append(f"### {accession} ({source})")
            report_lines.append(f"- Status: {status}")
            report_lines.append(f"- Files: {len(files)}")
            if result.get('errors'):
                report_lines.append(f"- Errors: {', '.join(result['errors'])}")

    report_lines.append("\n## Next Steps")
    report_lines.append("\nUse OmicsClaw skills for downstream analysis:")
    report_lines.append("- For single-cell preprocessing: `oc run sc-preprocessing --input <file> --output <dir>`")
    report_lines.append("- For spatial analysis: `oc run spatial-preprocess --input <file> --output <dir>`")

    report_file = output_dir / 'report.md'
    report_file.write_text('\n'.join(report_lines))


if __name__ == '__main__':
    main()
