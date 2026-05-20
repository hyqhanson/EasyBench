"""Download datasets from GEO, SRA, and cellxgene."""

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List

import requests


def download_geo_dataset(gse_id: str, output_dir: Path, max_retries: int = 3) -> Dict:
    """Download GEO dataset by GSE ID."""
    gse_id = gse_id.upper().strip()
    gse_dir = output_dir / gse_id
    gse_dir.mkdir(parents=True, exist_ok=True)

    result = {
        'gse_id': gse_id,
        'source': 'geo',
        'status': 'pending',
        'files': [],
        'errors': [],
    }

    try:
        metadata = fetch_geo_metadata(gse_id)
        if not metadata:
            result['status'] = 'failed'
            result['errors'].append(f"Could not fetch metadata for {gse_id}")
            return result

        metadata_file = gse_dir / 'metadata.json'
        metadata_file.write_text(json.dumps(metadata, indent=2))
        result['files'].append(str(metadata_file))

        supp_files = download_supplementary_files(gse_id, gse_dir, max_retries)
        result['files'].extend(supp_files)

        result['status'] = 'success' if result['files'] else 'partial'

    except Exception as e:
        result['status'] = 'failed'
        result['errors'].append(str(e))

    return result


def fetch_geo_metadata(gse_id: str) -> Dict:
    """Fetch GEO metadata via NCBI E-utilities."""
    try:
        url = f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={gse_id}&targ=self&form=text&view=quick"
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        text = response.text
        metadata = {
            'gse_id': gse_id,
            'title': extract_field(text, r'\!Series_title\s*=\s*(.+)'),
            'summary': extract_field(text, r'\!Series_summary\s*=\s*(.+)'),
            'organism': extract_field(text, r'\!Series_sample_organism\s*=\s*(.+)'),
            'platform': extract_field(text, r'\!Series_platform_id\s*=\s*(.+)'),
            'samples': extract_samples(text),
        }

        return metadata

    except Exception as e:
        print(f"Error fetching metadata for {gse_id}: {e}")
        return {}


def extract_field(text: str, pattern: str) -> str:
    """Extract field from GEO text format."""
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else ''


def extract_samples(text: str) -> List[str]:
    """Extract GSM sample IDs from GEO metadata."""
    pattern = r'\!Series_sample_id\s*=\s*(GSM\d+)'
    return re.findall(pattern, text)


def download_supplementary_files(gse_id: str, output_dir: Path, max_retries: int = 3) -> List[str]:
    """Download supplementary files from GEO FTP."""
    downloaded = []

    try:
        gse_prefix = gse_id[:-3] + 'nnn'
        ftp_base = f"https://ftp.ncbi.nlm.nih.gov/geo/series/{gse_prefix}/{gse_id}/suppl/"

        response = requests.get(ftp_base, timeout=30)
        if response.status_code != 200:
            return downloaded

        file_links = re.findall(r'href="([^"]+\.(h5ad|mtx|csv|tsv|txt|gz|tar))"', response.text, re.IGNORECASE)

        for filename, _ in file_links[:10]:
            file_url = ftp_base + filename
            output_file = output_dir / filename

            if _download_file(file_url, output_file, max_retries):
                downloaded.append(str(output_file))

    except Exception as e:
        print(f"Error downloading supplementary files: {e}")

    return downloaded


def download_sra_dataset(sra_id: str, output_dir: Path, max_retries: int = 3, download_fastq: bool = False) -> Dict:
    """Download metadata and candidate files for an SRA accession."""
    sra_id = sra_id.upper().strip()
    sra_dir = output_dir / sra_id
    sra_dir.mkdir(parents=True, exist_ok=True)

    result = {
        'sra_id': sra_id,
        'source': 'sra',
        'status': 'pending',
        'files': [],
        'errors': [],
    }

    try:
        metadata = fetch_sra_metadata(sra_id)
        if not metadata:
            result['status'] = 'failed'
            result['errors'].append(f"Could not fetch metadata for {sra_id}")
            return result

        metadata_file = sra_dir / 'metadata.json'
        metadata_file.write_text(json.dumps(metadata, indent=2))
        result['files'].append(str(metadata_file))

        if download_fastq:
            for run_accession in metadata.get('runs', [])[:5]:
                file_url = _sra_accession_to_ftp_url(run_accession)
                output_file = sra_dir / f"{run_accession}.sra"
                if _download_file(file_url, output_file, max_retries):
                    result['files'].append(str(output_file))

        result['status'] = 'success' if result['files'] else 'partial'

    except Exception as e:
        result['status'] = 'failed'
        result['errors'].append(str(e))

    return result


def fetch_sra_metadata(sra_id: str) -> Dict:
    """Fetch SRA metadata via NCBI E-utilities."""
    try:
        url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=sra&id={sra_id}&retmode=xml"
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        root = ET.fromstring(response.text)
        title = root.findtext('.//EXPERIMENT/TITLE') or root.findtext('.//SAMPLE/TITLE') or ''
        organism = root.findtext('.//ORGANISM') or ''
        runs = [run.attrib.get('accession') for run in root.findall('.//RUN') if 'accession' in run.attrib]

        return {
            'sra_id': sra_id,
            'title': title,
            'organism': organism,
            'runs': list(dict.fromkeys(runs)),
        }

    except Exception as e:
        print(f"Error fetching SRA metadata for {sra_id}: {e}")
        return {}


def _sra_accession_to_ftp_url(accession: str) -> str:
    prefix = accession[:3]
    return f"https://ftp.ncbi.nlm.nih.gov/sra/sra-instant/reads/ByRun/sra/{prefix}/{accession}/{accession}.sra"


def download_cellxgene_dataset(dataset_id: str, output_dir: Path, max_retries: int = 3) -> Dict:
    """Download metadata and candidate files for a cellxgene dataset."""
    dataset_id = dataset_id.strip()
    slug = normalize_cellxgene_id(dataset_id)
    dataset_dir = output_dir / slug
    dataset_dir.mkdir(parents=True, exist_ok=True)

    result = {
        'cellxgene_id': slug,
        'source': 'cellxgene',
        'status': 'pending',
        'files': [],
        'errors': [],
    }

    try:
        metadata = fetch_cellxgene_metadata(slug)
        if not metadata:
            result['status'] = 'failed'
            result['errors'].append(f"Could not fetch metadata for {slug}")
            return result

        metadata_file = dataset_dir / 'metadata.json'
        metadata_file.write_text(json.dumps(metadata, indent=2))
        result['files'].append(str(metadata_file))

        for url in metadata.get('download_urls', [])[:5]:
            filename = url.split('/')[-1].split('?')[0]
            output_file = dataset_dir / filename
            if _download_file(url, output_file, max_retries):
                result['files'].append(str(output_file))

        result['status'] = 'success' if result['files'] else 'partial'

    except Exception as e:
        result['status'] = 'failed'
        result['errors'].append(str(e))

    return result


def normalize_cellxgene_id(value: str) -> str:
    value = value.strip()
    match = re.search(r'/d/([A-Za-z0-9_\-]+)', value)
    if match:
        return match.group(1)
    return value


def fetch_cellxgene_metadata(slug: str) -> Dict:
    """Fetch metadata and candidate download URLs from cellxgene pages."""
    page_url = f"https://cellxgene.cziscience.com/d/{slug}"
    metadata = {'dataset_slug': slug, 'page_url': page_url, 'download_urls': []}

    try:
        response = requests.get(page_url, timeout=30)
        response.raise_for_status()
        html = response.text

        download_urls = set(re.findall(r'https?://[^"\']+\.(?:h5ad|cxg|zip|tar\.gz|json)', html, re.IGNORECASE))
        metadata['download_urls'] = sorted(download_urls)
        metadata['title'] = slug

        return metadata

    except Exception as e:
        print(f"Error fetching cellxgene metadata for {slug}: {e}")
        return {}


def _download_file(url: str, output_file: Path, max_retries: int = 3) -> bool:
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=120, stream=True)
            response.raise_for_status()
            with output_file.open('wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f"Downloaded: {output_file}")
            return True
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"Failed to download {url}: {e}")
            else:
                time.sleep(2 ** attempt)
    return False
