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
    """Create a requests Session with optional proxy support.

    Proxy priority (highest first):
      1. EASYBENCH_PROXY env var (e.g. ``http://127.0.0.1:7890``)
      2. EASYBENCH_SOCKS5_PROXY env var (e.g. ``socks5://127.0.0.1:7890``)
      3. No proxy (direct connection, ``trust_env=False``)

    Note: Setting an env var is the recommended approach to avoid
    hardcoding proxy URLs in the codebase.
    """
    import os as _os
    session = requests.Session()
    proxy = _os.environ.get('EASYBENCH_PROXY', '').strip()
    socks = _os.environ.get('EASYBENCH_SOCKS5_PROXY', '').strip()
    if proxy:
        session.proxies = {'http': proxy, 'https': proxy}
    elif socks:
        session.proxies = {'http': socks, 'https': socks}
    else:
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
    """Download metadata and candidate data files for an SRA or BioProject accession.

    Supports:
      - SRA accessions (SRP, SRX, SRR, SRS)
      - BioProject IDs (PRJNA, PRJEB, PRJDB) — auto-resolved to SRA records

    By default downloads metadata + any available supplementary files
    (e.g. processed count matrices, .h5ad, .rds) from the SRA run selector
    page.  Set *download_fastq=True* to also download raw .sra files
    (huge — 10+ GB per run).
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

        # ── Download processed supplementary files (like GEO does) ──
        runs = metadata.get('runs', [])
        if runs:
            # Try to download supplementary files from the first few runs
            for run_acc in runs[:3]:
                supp_files = _download_sra_supplementary(run_acc, sra_dir, max_retries)
                if supp_files:
                    result['files'].extend(supp_files)
                    break  # Stop after the first successful run

        # ── Optional: download raw FASTQ ──
        if download_fastq:
            for run_accession in runs[:5]:
                file_url = _sra_accession_to_ftp_url(run_accession)
                output_file = sra_dir / f"{run_accession}.sra"
                if _download_file(file_url, output_file, max_retries):
                    result['files'].append(str(output_file))

        result['status'] = 'success' if len(result['files']) > 1 else (
            'partial' if result['files'] else 'failed'
        )

    except Exception as e:
        result['status'] = 'failed'
        result['errors'].append(str(e))

    return result


def _download_sra_supplementary(run_acc: str, output_dir: Path, max_retries: int = 3) -> List[str]:
    """Try to download processed data files from SRA run supplementary directory.

    Many SRA runs have attached supplementary files (e.g. .h5ad, Seurat .rds,
    count matrices) accessible via the NCBI SRA run selector FTP.
    URL pattern: https://sra-download.ncbi.nlm.nih.gov/traces/sra/{prefix}/{run_acc}/
    """
    downloaded = []
    prefix = run_acc[:3] + '/' + run_acc[:6]
    try:
        base_url = f"https://sra-download.ncbi.nlm.nih.gov/traces/sra/{prefix}/{run_acc}/"
        session = _get_session()
        resp = session.get(base_url, timeout=30)
        if resp.status_code != 200:
            return downloaded

        # Parse NCBI directory listing
        links = re.findall(r'href="([^"]+)"', resp.text)
        for fname in links:
            if fname in ('/', '..', 'Parent Directory') or fname.startswith('/'):
                continue
            # Skip raw SRA archives and FASTQ files (huge)
            if fname.endswith('.sra') or fname.endswith('.fastq') or fname.endswith('.fastq.gz'):
                continue
            file_url = base_url + fname
            out_file = output_dir / fname
            if _download_file(file_url, out_file, max_retries):
                downloaded.append(str(out_file))
    except Exception:
        pass
    return downloaded


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

    # Extract record ID from various formats:
    #   - https://zenodo.org/records/15007208
    #   - https://doi.org/10.5281/zenodo.18674907
    #   - 17259745 (bare number)
    record_match = (_re.search(r'zenodo\.org/records/(\d+)', zenodo_url) or
                    _re.search(r'zenodo[.](\d+)', zenodo_url))
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


def clone_github_repo(repo_url: str, output_dir: Path, depth: int = 1, timeout: int = 300) -> Dict:
    """Clone a GitHub repository to a local directory.

    Skips if the directory already exists and is a git repo (idempotent).
    If the URL is NOT a valid repo (e.g. an organisation homepage), returns
    ``status='invalid_url'`` with the error reason.

    Parameters
    ----------
    repo_url:
        Full GitHub URL (e.g. ``https://github.com/user/repo``).
    output_dir:
        Local directory to clone into (parent). The repo will be cloned
        into ``output_dir/{repo_name}/``.
    depth:
        ``--depth`` for shallow clone (default 1).
    timeout:
        Max seconds to wait for git clone.

    Returns
    -------
    dict with ``repo_url``, ``status``, ``clone_path``, ``branch``, ``errors``.
    """
    repo_name = repo_url.rstrip('/').split('/')[-1]
    if repo_name.endswith('.git'):
        repo_name = repo_name[:-4]
    clone_path = output_dir / repo_name

    result = {
        'repo_url': repo_url,
        'source': 'github',
        'status': 'pending',
        'clone_path': str(clone_path),
        'branch': '',
        'errors': [],
    }

    # ── Pre-flight: verify the URL is an actual repo (not an org homepage) ──
    # An org homepage like https://github.com/OrgName has no repo name after the org
    parts = repo_url.rstrip('/').split('/')
    if len(parts) < 5 or (len(parts) == 5 and parts[-1] == parts[-2]):
        result['status'] = 'invalid_url'
        msg = f"URL appears to be an organisation homepage, not a repo: {repo_url}"
        result['errors'].append(msg)
        print(f'    ⚠️  {msg}')
        return result

    try:
        r = subprocess.run(
            ['git', 'ls-remote', '--heads', repo_url],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            stderr = (r.stderr or '').strip()
            result['status'] = 'invalid_url'
            result['errors'].append(f'git ls-remote failed: {stderr[:300]}')
            print(f'    ⚠️  [{repo_name}] Not a valid git repo: {stderr[:200]}')
            return result
    except subprocess.TimeoutExpired:
        result['status'] = 'failed'
        result['errors'].append('git ls-remote timed out')
        print(f'    ⚠️  [{repo_name}] git ls-remote timed out')
        return result
    except Exception as exc:
        result['errors'].append(f'git ls-remote error: {exc}')
        # Continue anyway — maybe it's a private repo

    # ── Idempotent: if already cloned, just fetch ──
    if (clone_path / '.git').exists():
        try:
            subprocess.run(
                ['git', 'fetch', '--depth', str(depth), 'origin'],
                cwd=str(clone_path), capture_output=True, timeout=min(timeout, 60),
            )
            br = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=str(clone_path), capture_output=True, text=True, timeout=15,
            )
            result['branch'] = br.stdout.strip()
            result['status'] = 'success'
            return result
        except Exception as exc:
            result['errors'].append(f'fetch failed: {exc}')

    # ── Fresh clone ──
    cmd = ['git', 'clone', '--depth', str(depth), repo_url, str(clone_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            stderr = (proc.stderr or '').strip()
            result['status'] = 'failed'
            result['errors'].append(f'git clone failed: {stderr[:300]}')
            print(f'    ❌ [{repo_name}] Clone failed: {stderr[:200]}')
            return result

        # Get branch
        br = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=str(clone_path), capture_output=True, text=True, timeout=15,
        )
        result['branch'] = br.stdout.strip()
        result['status'] = 'success'
    except subprocess.TimeoutExpired:
        result['status'] = 'failed'
        result['errors'].append('clone timed out')
        print(f'    ❌ [{repo_name}] Clone timed out after {timeout}s')
    except Exception as exc:
        result['status'] = 'failed'
        result['errors'].append(str(exc))
        print(f'    ❌ [{repo_name}] Clone error: {exc}')

    return result


def download_generic_code(url: str, output_dir: Path) -> Dict:
    """Download code from a generic URL (non-GitHub, non-Zenodo).

    Handles URLs like ``keeper.mpdl.mpg.de``, ``figshare.com`` (non-DOI),
    institutional repositories, and direct file downloads.
    Saves to ``output_dir/code_{sanitized_name}/``.
    """
    import re as _re
    url = url.strip()
    sanitized = _re.sub(r'[^\w\-]+', '_', url.split('//')[-1].split('?')[0])[:40]
    dest_dir = output_dir / f'code_{sanitized}'
    dest_dir.mkdir(parents=True, exist_ok=True)

    result = {
        'url': url,
        'source': 'generic',
        'status': 'pending',
        'path': str(dest_dir),
        'errors': [],
    }

    try:
        resp = _get(url, timeout=60, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '')

        cd = resp.headers.get('Content-Disposition', '')
        filename = None
        if 'filename=' in cd:
            import cgi
            _, params = cgi.parse_header(cd)
            filename = params.get('filename', None)
        if not filename:
            filename = url.rstrip('/').split('/')[-1].split('?')[0] or 'download'

        out_file = dest_dir / filename
        with out_file.open('wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        meta = {
            'url': url,
            'content_type': content_type,
            'filename': filename,
            'file_size': out_file.stat().st_size,
        }
        (dest_dir / 'download_info.json').write_text(json.dumps(meta, indent=2))
        result['status'] = 'success'
    except Exception as exc:
        result['status'] = 'failed'
        result['errors'].append(str(exc))
        print(f'    ⚠️  Generic code download failed {url}: {exc}')

    return result


def unpack_data_files(data_root, unpack_root=None):
    """Recursively extract all tar/zip archives under data_root.

    Extracted contents go to ``unpack_root/`` (defaults to
    ``data_root/../unpacked_data/``), preserving relative paths.

    Returns ``{unpacked, failed, skipped, details: [...]}``.
    """
    import subprocess as _sp
    from pathlib import Path as _P

    if unpack_root is None:
        unpack_root = _P(data_root).parent / 'unpacked_data'
    unpack_root = _P(unpack_root)
    unpack_root.mkdir(parents=True, exist_ok=True)

    archives = []
    for pattern in ('*.tar', '*.tar.gz', '*.tgz', '*.zip'):
        archives.extend(list(_P(data_root).rglob(pattern)))
    # Also collect single-file .gz (exclude .tar.gz/.tgz already picked above)
    gz_files = []
    for gz in _P(data_root).rglob('*.gz'):
        if gz.name.endswith('.tar.gz') or gz.name.endswith('.tgz'):
            continue
        gz_files.append(gz)

    summary = {'unpacked': 0, 'failed': 0, 'skipped': 0, 'details': []}

    for arc in archives:
        rel = arc.relative_to(data_root)
        # Strip archive extension(s) for destination folder name
        name = arc.name
        for sfx in ('.tar.gz', '.tgz', '.tar', '.zip'):
            if name.endswith(sfx):
                name = name[:-len(sfx)]
                break
        dest = unpack_root / rel.parent

        if dest.exists() and any(dest.iterdir()):
            summary['skipped'] += 1
            continue

        dest.mkdir(parents=True, exist_ok=True)
        try:
            if arc.suffix == '.zip':
                _sp.run(
                    ['powershell', '-Command',
                     'Expand-Archive -Path ' + repr(str(arc)) +
                     ' -DestinationPath ' + repr(str(dest)) + ' -Force'],
                    capture_output=True, timeout=300, check=True,
                )
            else:
                _sp.run(
                    ['tar', '-xf', str(arc), '-C', str(dest)],
                    capture_output=True, timeout=300, check=True,
                )
            print('  unpacked:', str(rel))
            summary['unpacked'] += 1
            summary['details'].append({
                'file': str(rel), 'status': 'ok', 'dest': str(dest),
            })
        except Exception as exc:
            print('  FAILED:', str(rel), '-', str(exc))
            summary['failed'] += 1
            summary['details'].append({
                'file': str(rel), 'status': 'failed', 'error': str(exc),
            })

    # Decompress single .gz files (e.g., .txt.gz, .csv.gz, .mtx.gz)
    for gz in gz_files:
        rel = gz.relative_to(data_root)
        out_name = gz.name[:-3]  # strip .gz
        dest_file = unpack_root / rel.parent / out_name

        if dest_file.exists() and dest_file.stat().st_size > 0:
            summary['skipped'] += 1
            continue

        dest_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            import gzip as _gzip, shutil as _shutil
            with _gzip.open(gz, 'rb') as f_in:
                with open(dest_file, 'wb') as f_out:
                    _shutil.copyfileobj(f_in, f_out)
            print('  unpacked:', str(rel))
            summary['unpacked'] += 1
            summary['details'].append({
                'file': str(rel), 'status': 'ok', 'dest': str(dest_file),
            })
        except Exception as exc:
            print('  FAILED:', str(rel), '-', str(exc))
            summary['failed'] += 1
            summary['details'].append({
                'file': str(rel), 'status': 'failed', 'error': str(exc),
            })

    # ── Recursive pass: unpacked dirs may contain *new* archives from tar extraction ──
    # e.g. GSE285933_RAW.tar → GSE285933/GSM*.CEL.gz  (need another pass)
    summary['sub_unpacked'] = 0
    for entry in sorted(unpack_root.rglob('*')):
        if not entry.is_file():
            continue
        ext = entry.suffix.lower()
        if ext in ('.tar', '.zip') or entry.name.endswith('.tar.gz') or entry.name.endswith('.tgz'):
            sub_result = unpack_data_files(unpack_root, unpack_root)
            summary['sub_unpacked'] += sub_result.get('unpacked', 0)
        elif ext == '.gz' and not entry.name.endswith('.tar.gz') and not entry.name.endswith('.tgz'):
            # Already-compressed files inside unpacked dirs — decompress
            out_name = entry.name[:-3]
            dest_file = entry.parent / out_name
            if dest_file.exists() and dest_file.stat().st_size > 0:
                continue
            try:
                import gzip as _gz, shutil as _sh
                with _gz.open(entry, 'rb') as f_in:
                    out_tmp = entry.parent / ('._tmp_' + out_name)
                    with open(out_tmp, 'wb') as f_out:
                        _sh.copyfileobj(f_in, f_out)
                    out_tmp.rename(dest_file)  # atomic
                print(f'  unpacked (recursive): {entry.relative_to(unpack_root)}')
                summary['unpacked'] += 1
            except Exception as exc:
                pass  # binary files fail silently

    # ── Also copy non-compressed data files (rds, h5ad, tsv, csv, etc.) ──
    # These already exist in data/ but not in unpacked_data/
    data_root_p = _P(data_root)
    for data_file in data_root_p.rglob('*'):
        if not data_file.is_file():
            continue
        ext = data_file.suffix.lower()
        # Skip compressed/archive files (already handled above)
        if ext in ('.gz', '.zip', '.tar', '.tgz', '.bz2', '.xz'):
            continue
        # Skip hidden/temp files
        if data_file.name.startswith('._') or data_file.name.startswith('.'):
            continue
        rel = data_file.relative_to(data_root_p)
        dest = unpack_root / rel.parent / data_file.name
        if dest.exists() and dest.stat().st_size > 0:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            import shutil as _sh
            _sh.copy2(data_file, dest)
            print(f'  copied: {str(rel)}')
            summary['copied'] = summary.get('copied', 0) + 1
            summary['details'].append({
                'file': str(rel), 'status': 'copied', 'dest': str(dest),
            })
        except Exception as exc:
            print(f'  FAILED copy: {str(rel)} - {exc}')
            summary['details'].append({
                'file': str(rel), 'status': 'failed_copy', 'error': str(exc),
            })

    # ── Cleanup: remove decompressed .gz and .tar from unpacked_data ──
    cleaned = 0
    for entry in sorted(unpack_root.rglob('*')):
        if not entry.is_file():
            continue
        name = entry.name.lower()
        # Determine if this file has a decompressed counterpart
        if name.endswith('.gz') and not name.endswith('.tar.gz'):
            uncompressed = entry.parent / name[:-3]
            if uncompressed.exists() and uncompressed.stat().st_size > 0:
                entry.unlink()
                cleaned += 1
        elif any(name.endswith(sfx) for sfx in ('.tar', '.zip', '.tar.gz', '.tgz')):
            # Check if the archive was extracted
            extracted_dir = entry.parent / name.rsplit('.', 1)[0]
            if extracted_dir.exists() and any(extracted_dir.iterdir()):
                entry.unlink()
                cleaned += 1
    if cleaned:
        print(f'  Cleaned up {cleaned} original compressed file(s) from unpacked_data')

    return summary
