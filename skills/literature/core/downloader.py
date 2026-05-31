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
    """Download metadata and data files for a cellxgene dataset or collection.

    *dataset_id* accepts:
      - Bare dataset UUID  (e.g. ``329c91df-1383-4dea-8e29-5b5f25da9178``)
      - Collection URL     (e.g. ``https://cellxgene.cziscience.com/collections/{uuid}``)
      - Direct .h5ad URL   (e.g. ``https://datasets.cellxgene.cziscience.com/{uuid}.h5ad``)
    """
    dataset_id = dataset_id.strip()
    normalized = normalize_cellxgene_id(dataset_id)

    # If it's a direct download URL, use the UUID as folder name
    if normalized.startswith('http') and normalized.endswith('.h5ad'):
        folder = re.sub(r'[^\w\-]+', '_', normalized.split('/')[-1].replace('.h5ad', ''))
    else:
        folder = normalized

    dataset_dir = output_dir / folder
    dataset_dir.mkdir(parents=True, exist_ok=True)

    result = {
        'cellxgene_id': normalized,
        'source': 'cellxgene',
        'status': 'pending',
        'files': [],
        'errors': [],
    }

    try:
        metadata = fetch_cellxgene_metadata(dataset_id)
        if not metadata or not metadata.get('download_urls'):
            result['status'] = 'failed'
            result['errors'].append(f"Could not fetch metadata for {dataset_id}")
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


UUID_PATTERN = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
CELLXGENE_API = 'https://api.cellxgene.cziscience.com'
DIRECT_DL_BASE = 'https://datasets.cellxgene.cziscience.com'


def normalize_cellxgene_id(value: str) -> str:
    """Normalise a cellxgene identifier to a plain UUID or direct .h5ad URL.

    Accepts:
      - Bare UUID (329c91df-...)
      - Collection URL (https://cellxgene.cziscience.com/collections/{uuid})
      - Direct download URL (https://datasets.cellxgene.cziscience.com/{uuid}.h5ad)
      - Legacy dataset page (/d/{slug})
    """
    value = value.strip()

    # Already a direct .h5ad URL → return as-is
    if value.startswith('http') and value.endswith('.h5ad'):
        return value

    # Extract UUID from any cellxgene URL
    m = re.search(rf'({UUID_PATTERN})', value, re.IGNORECASE)
    if m:
        return m.group(1).lower()

    # Legacy /d/{slug}
    m = re.search(r'/d/([A-Za-z0-9_\-]+)', value)
    if m:
        return m.group(1)

    # Maybe it's already a plain UUID without hyphens? (unlikely but safe)
    m = re.search(r'\b([0-9a-fA-F]{32})\b', value)
    if m:
        raw = m.group(1)
        return f'{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}'

    return value


def fetch_cellxgene_metadata(identifier: str) -> Dict:
    """Fetch metadata and download URLs from cellxgene.

    *identifier* can be:
      - A dataset UUID (constructs direct download URL)
      - A collection UUID (fetches collection API to list datasets)
      - A legacy /d/{slug} (scrapes the old dataset page)
      - A direct .h5ad URL (used as-is)
    """
    meta: Dict = {
        'id': identifier,
        'title': '',
        'page_url': '',
        'download_urls': [],
        'is_collection': False,
    }

    # --- Case 1: already a direct .h5ad URL ---
    if identifier.startswith('http') and identifier.endswith('.h5ad'):
        meta['download_urls'] = [identifier]
        meta['page_url'] = identifier
        return meta

    # --- Case 2: collection UUID (via API) ---
    coll_match = re.search(rf'collections/({UUID_PATTERN})', identifier, re.IGNORECASE)
    if coll_match or _looks_like_uuid(identifier):
        uuid = (coll_match.group(1) if coll_match else identifier).lower()
        meta['id'] = uuid
        meta['page_url'] = f'https://cellxgene.cziscience.com/collections/{uuid}'

        # First try collection API
        try:
            resp = requests.get(f'{CELLXGENE_API}/dp/v1/collections/{uuid}', timeout=30)
            if resp.ok:
                data = resp.json()
                meta['title'] = data.get('name', '') or data.get('title', '')
                datasets = data.get('datasets', data.get('collections', []))
                for ds in datasets:
                    ds_id = ds.get('dataset_id', ds.get('id', ''))
                    if ds_id:
                        meta['download_urls'].append(f'{DIRECT_DL_BASE}/{ds_id}.h5ad')
                if meta['download_urls']:
                    return meta
        except Exception:
            pass

        # Fallback: treat the uuid as a single dataset ID
        try:
            resp = requests.get(f'{CELLXGENE_API}/dp/v1/datasets/{uuid}', timeout=30)
            if resp.ok:
                data = resp.json()
                meta['title'] = data.get('name', '') or data.get('title', '')
                for asset in data.get('assets', []):
                    url = asset.get('url', asset.get('file_url', ''))
                    if url:
                        meta['download_urls'].append(url)
                if meta['download_urls']:
                    return meta
        except Exception:
            pass

        # Final fallback: construct direct download URL
        meta['download_urls'] = [f'{DIRECT_DL_BASE}/{uuid}.h5ad']
        return meta

    # --- Case 3: legacy /d/{slug} (scrape) ---
    slug = identifier
    page_url = f'https://cellxgene.cziscience.com/d/{slug}'
    meta['page_url'] = page_url
    try:
        resp = requests.get(page_url, timeout=30)
        resp.raise_for_status()
        html = resp.text
        meta['title'] = slug
        urls = set(re.findall(r'https?://[^"\']+\.(?:h5ad|cxg|zip|tar\.gz|json)', html, re.IGNORECASE))
        meta['download_urls'] = sorted(urls)
    except Exception as e:
        print(f'Error fetching cellxgene legacy page for {slug}: {e}')

    return meta


def _looks_like_uuid(value: str) -> bool:
    """Check if a string looks like a UUID (with or without hyphens)."""
    return bool(re.match(rf'^{UUID_PATTERN}$', value, re.IGNORECASE))


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
