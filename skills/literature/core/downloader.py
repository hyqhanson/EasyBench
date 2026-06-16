"""Download datasets from GEO, SRA, Zenodo, and cellxgene."""

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List

import requests
import urllib3

# Disable InsecureRequestWarning for verify=False fallbacks
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _get_session():
    """Create a requests Session that bypasses the system proxy.

    System proxies (e.g. Clash, V2Ray) can interfere with NCBI/Zenodo
    API calls.  Using ``trust_env=False`` routes requests directly.
    """
    session = requests.Session()
    session.trust_env = False
    return session


def _get(url: str, timeout: int = 30, stream: bool = False):
    """Perform a GET request bypassing the system proxy."""
    session = _get_session()
    return session.get(url, timeout=timeout, stream=stream)


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
        session = _get_session()
        response = session.get(url, timeout=30)
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


def download_supplementary_files(gse_id: str, output_dir: Path, max_retries: int = 3,
                              max_total_gb: float = 20.0) -> List[str]:
    """Download supplementary files from GEO FTP.

    Files are prioritized: count matrices (.mtx, .h5ad, .h5, .tar) come
    first, followed by annotation files (.csv, .tsv).  A total download
    size budget (default 20 GB) prevents runaway downloads.
    """
    downloaded = []

    try:
        gse_prefix = gse_id[:-3] + 'nnn'
        ftp_base = f"https://ftp.ncbi.nlm.nih.gov/geo/series/{gse_prefix}/{gse_id}/suppl/"

        session = _get_session()
        response = session.get(ftp_base, timeout=30)
        if response.status_code != 200:
            return downloaded

        # Parse file listing: extract (filename, size_bytes)
        # File rows look like: <a href="file.mtx.gz">file.mtx.gz</a>  2023-03-28 10:10  3.5G
        file_entries = []
        for line in response.text.split('\n'):
            # Extract href
            href_m = re.search(r'href="([^"]+)"', line)
            if not href_m:
                continue
            filename = href_m.group(1)
            # Skip navigation links
            if not filename or filename == '/' or 'Parent Directory' in line:
                continue
            if filename.startswith('/'):
                continue

            # Extract size from the last column (e.g. "3.5G", "519K", "   -  ")
            size = 0
            size_m = re.search(r'([\d.]+)\s*([KMGT])?\s*$', line.strip())
            if size_m:
                try:
                    size_val = float(size_m.group(1))
                    unit = (size_m.group(2) or 'B').upper()
                    if unit == 'K':
                        size = int(size_val * 1024)
                    elif unit == 'M':
                        size = int(size_val * 1024 ** 2)
                    elif unit == 'G':
                        size = int(size_val * 1024 ** 3)
                    elif unit == 'T':
                        size = int(size_val * 1024 ** 4)
                except ValueError:
                    pass

            file_entries.append((filename, size))

        # Fallback: simpler regex if the size parsing didn't work
        if not file_entries:
            links = re.findall(r'href="([^"]+)"', response.text)
            for fname in links:
                if fname in ('Parent Directory', '/', ''):
                    continue
                if fname.startswith('/'):
                    continue
                file_entries.append((fname, 0))

        # Priority: data files first, then annotation/metadata
        _DATA_EXT = {'.mtx', '.h5ad', '.h5', '.loom', '.tar', '.rds', '.h5seurat'}
        def _is_data_file(fname):
            low = fname.lower()
            # Handle compound extensions like .mtx.gz, .csv.gz
            for ext in _DATA_EXT:
                if low.endswith(ext) or low.endswith(ext + '.gz'):
                    return True
            return False

        data_files = [(f, s) for f, s in file_entries if _is_data_file(f)]
        other_files = [(f, s) for f, s in file_entries if not _is_data_file(f)]

        max_bytes = int(max_total_gb * 1024 ** 3)
        total_downloaded = 0

        # Download data files first, then others
        for filename, fsize in data_files + other_files:
            # Skip if we'd exceed the budget (only for known-size files)
            if fsize > 0 and total_downloaded + fsize > max_bytes:
                print(f"  Skipping {filename} ({fsize / 1e9:.1f} GB) — would exceed {max_total_gb} GB budget")
                continue

            file_url = ftp_base + filename
            output_file = output_dir / filename

            if _download_file(file_url, output_file, max_retries):
                downloaded.append(str(output_file))
                # Update size from actual file
                if output_file.exists():
                    total_downloaded += output_file.stat().st_size

    except Exception as e:
        print(f"Error downloading supplementary files: {e}")

    return downloaded


def download_sra_dataset(sra_id: str, output_dir: Path, max_retries: int = 3, download_fastq: bool = False) -> Dict:
    """Download metadata and candidate files for an SRA or BioProject accession.

    Supports:
      - SRA accessions (SRP, SRX, SRR, SRS)
      - BioProject IDs (PRJNA, PRJEB, PRJDB) — auto-resolved to SRA records
    """
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


def _is_bioproject_id(accession: str) -> bool:
    """Check if an accession is a BioProject ID (PRJNA/PRJEB/PRJDB prefix)."""
    return bool(re.match(r'^(PRJNA|PRJEB|PRJDB)\d+$', accession, re.IGNORECASE))


def _resolve_bioproject_to_sra_ids(bioproject_id: str) -> List[str]:
    """Resolve a BioProject ID to its SRA experiment/study accessions.

    Returns a list of UID strings that can be used with db=sra efetch.
    """
    try:
        # Step 1: search SRA for records linked to this BioProject
        session = _get_session()
        search_url = (
            f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            f"?db=sra&term={bioproject_id}[All Fields]&retmax=500&retmode=xml"
        )
        response = session.get(search_url, timeout=30)
        response.raise_for_status()
        root = ET.fromstring(response.text)
        uid_list = root.findall('.//IdList/Id')
        sra_uids = [uid.text for uid in uid_list if uid.text]
        if sra_uids:
            print(f"  Resolved {bioproject_id} → {len(sra_uids)} SRA record(s)")
        return sra_uids
    except Exception as e:
        print(f"Error resolving BioProject {bioproject_id} to SRA: {e}")
        return []


def fetch_sra_metadata(sra_id: str) -> Dict:
    """Fetch SRA metadata via NCBI E-utilities.

    Supports:
      - SRA accessions (SRP, SRX, SRR, SRS)
      - BioProject IDs (PRJNA, PRJEB, PRJDB) — auto-resolved to SRA records
    """
    try:
        actual_id = sra_id

        # Handle BioProject IDs: resolve to SRA UIDs first
        if _is_bioproject_id(sra_id):
            sra_uids = _resolve_bioproject_to_sra_ids(sra_id)
            if not sra_uids:
                print(f"Could not resolve BioProject {sra_id} to any SRA records")
                return {}
            # Use the first batch of UIDs (NCBI accepts comma-separated)
            actual_id = ','.join(sra_uids[:20])

        url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=sra&id={actual_id}&retmode=xml"
        session = _get_session()
        response = session.get(url, timeout=60)
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
            resp = _get(f'{CELLXGENE_API}/dp/v1/collections/{uuid}', timeout=30)
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
            resp = _get(f'{CELLXGENE_API}/dp/v1/datasets/{uuid}', timeout=30)
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
        resp = _get(page_url, timeout=30)
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


def download_from_zenodo(zenodo_url: str, output_dir: Path, max_retries: int = 3) -> Dict:
    """Download dataset from a Zenodo record (DOI or record URL).

    Accepts:
      - Zenodo DOI: ``https://doi.org/10.5281/zenodo.17259745``
      - Record URL: ``https://zenodo.org/records/17259745``
      - Record ID:  ``17259745``
    """
    import re as _re

    zenodo_url = zenodo_url.strip()

    # Extract record ID from various formats
    record_match = _re.search(r'zenodo[./](\d+)', zenodo_url)
    if not record_match:
        # Try as a bare number
        if _re.match(r'^\d+$', zenodo_url):
            record_id = zenodo_url
        else:
            return {
                'zenodo_id': zenodo_url,
                'source': 'zenodo',
                'status': 'failed',
                'files': [],
                'errors': [f'Could not extract Zenodo record ID from: {zenodo_url}'],
            }
    else:
        record_id = record_match.group(1)

    record_dir = output_dir / f'zenodo_{record_id}'
    record_dir.mkdir(parents=True, exist_ok=True)

    result = {
        'zenodo_id': record_id,
        'source': 'zenodo',
        'status': 'pending',
        'files': [],
        'errors': [],
    }

    try:
        # Fetch record metadata via Zenodo API
        api_url = f'https://zenodo.org/api/records/{record_id}'
        response = _get(api_url, timeout=30)
        response.raise_for_status()
        record_data = response.json()

        title = record_data.get('metadata', {}).get('title', '')
        result['title'] = title
        files = record_data.get('files', [])

        # Save metadata
        metadata = {
            'record_id': record_id,
            'title': title,
            'doi': record_data.get('doi', ''),
            'files': [
                {'key': f.get('key', ''), 'size': f.get('size', 0)}
                for f in files
            ],
        }
        metadata_file = record_dir / 'metadata.json'
        metadata_file.write_text(json.dumps(metadata, indent=2))
        result['files'].append(str(metadata_file))

        # Download files (skip PDF-only records if they're just supplementary notes)
        data_files = [f for f in files if f.get('key', '').lower().endswith(('.h5ad', '.h5', '.mtx', '.csv', '.tsv', '.tar', '.gz', '.zip', '.loom', '.rds'))]
        if not data_files:
            # If no data files found, still download whatever is available (e.g. .pdf may contain data links)
            data_files = files

        for fobj in data_files[:20]:  # Cap at 20 files
            file_url = fobj.get('links', {}).get('self', '')
            raw_filename = fobj.get('key', '')
            if not file_url or not raw_filename:
                continue

            # Sanitize: replace / with _ (Zenodo folders use / in keys)
            filename = raw_filename.replace('/', '_').replace('\\', '_')
            output_file = record_dir / filename
            print(f'  Downloading Zenodo file: {filename} ({fobj.get("size", 0) / 1e6:.1f} MB)')
            if _download_file(file_url, output_file, max_retries):
                result['files'].append(str(output_file))

        result['status'] = 'success' if len(result['files']) > 1 else 'partial'

    except Exception as e:
        result['status'] = 'failed'
        result['errors'].append(str(e))
        print(f"Error downloading from Zenodo {record_id}: {e}")

    return result


def _download_file(url: str, output_file: Path, max_retries: int = 3) -> bool:
    for attempt in range(max_retries):
        try:
            response = _get(url, timeout=120, stream=True)
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
