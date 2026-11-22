"""
Pluggable cloud storage for CSV exports + JSON config bundles.

Historically Google Cloud Storage only. Every public function here KEPT its
original GCS-flavoured name (`generate_csv_export_gcs`, the `_gcs_` prefix on the
enablement gate, `GCS_BUCKET_NAME`, ...) even though the module is now
backend-agnostic, because every caller across the codebase
(`runtime/tools/business.py`, `runtime/agents/business.py`,
`runtime/prod_bundle.py`, and the test suite) imports these exact names.
Renaming them would be a bigger, riskier change than adding a second backend
behind the same names.

Backend selection: `CLOUD_STORAGE_BACKEND`, which now only has one real value —
`"azure_blob"` (the default). See `shared/storage_backends/` for the interface
and the implementation.

--- The GCS backend was removed 2026-08-16 ---
This repo has been Azure-only since 2026-08-02, and `google-cloud-storage` is no
longer in `requirements.txt`. The default used to be `"gcs"`, which was the real
bug: prod set `CLOUD_STORAGE_BACKEND=azure_blob` out-of-band, but the
checked-in docker-compose files of the self-hosted demo stacks set nothing,
so those stacks silently fell back to a GCS backend whose credentials do not
exist on Azure compute. A stale `CLOUD_STORAGE_BACKEND=gcs` in some environment now logs a warning and uses
Azure Blob rather than failing on a missing SDK.

--- Expiry semantics — read before touching CSV_EXPIRATION_HOURS ---
The returned CSV URL is a user-delegation SAS whose expiry IS a real, enforced
deadline: the link 403s at exactly `now + CSV_EXPIRATION_HOURS`. (The removed GCS
backend never enforced this — `expires_at` was descriptive metadata and the
public URL kept working until an out-of-band bucket lifecycle rule deleted the
object.) See `shared/storage_backends/azure_backend.py` for the full writeup,
including the `AZURE_BLOB_PUBLIC_CONTAINER` opt-in that trades the hard deadline
back for permanent-until-deleted URLs.
"""

import json
import csv
import io
import uuid
import os
import concurrent.futures
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
import logging

from shared.storage_backends.azure_backend import AzureBlobBackend

logger = logging.getLogger(__name__)

# Legacy name kept because callers/tests import it; only the object-path prefix
# and the `GCS_*` env-var names survive from the GCS era.
GCS_BUCKET_NAME = os.getenv("GCS_CSV_BUCKET", "csv-exports")
CSV_EXPIRATION_HOURS = int(os.getenv("CSV_EXPIRATION_HOURS", "24"))  # 24 hours default

# --- Azure configuration ---
# No default for the account URL — it must be set explicitly for the CSV export
# to be enabled at all (see `_gcs_export_enabled`), so a stack that never
# configured blob storage degrades to "rows without a download link" rather than
# erroring. Example: https://<account>.blob.core.windows.net
AZURE_STORAGE_ACCOUNT_URL = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "")
AZURE_STORAGE_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER", "csv-exports")

# Hard ceiling on the *entire* cloud-storage interaction (client/credential init
# + upload + metadata + publish-URL). The CSV is only a download sidecar for the
# chat answer — it must never be allowed to block the request. If the backend is
# slow/stuck (e.g. a hung Managed Identity token endpoint) we abandon the upload
# and hand the row data back to the caller.
GCS_UPLOAD_TIMEOUT_SECONDS = int(os.getenv("GCS_UPLOAD_TIMEOUT_SECONDS", "8"))

_AZURE_BACKEND_ALIASES = ("azure", "azure_blob", "azureblob")
DEFAULT_BACKEND = "azure_blob"


def _backend_name() -> str:
    """Read fresh (not frozen at import) so tests / a future admin-portal toggle
    can flip `CLOUD_STORAGE_BACKEND` without reloading the module."""
    return (os.getenv("CLOUD_STORAGE_BACKEND") or DEFAULT_BACKEND).strip().lower()


def _get_backend():
    """Construct the active backend for this call.

    Azure Blob is the only backend. Any other value — including the retired
    `"gcs"` — warns and uses it anyway, because there is nothing else to fall
    back to and raising here would turn a stale env var into a broken CSV tool.

    Cheap and stateless: this only instantiates the small wrapper object
    (container name, expiration hours). The backend does not import or construct
    its SDK client here — that happens lazily, per call, inside the backend's own
    methods.
    """
    backend = _backend_name()
    if backend not in _AZURE_BACKEND_ALIASES:
        logger.warning(
            "cloud_storage: CLOUD_STORAGE_BACKEND=%r is not supported "
            "(the GCS backend was removed 2026-08-16) — using %s.",
            backend, DEFAULT_BACKEND,
        )
    return AzureBlobBackend(
        account_url=AZURE_STORAGE_ACCOUNT_URL,
        container_name=AZURE_STORAGE_CONTAINER,
        expiration_hours=CSV_EXPIRATION_HOURS,
    )


def _gcs_export_enabled() -> bool:
    """Decide whether to even attempt a cloud upload for the CSV sidecar.

    The CSV sidecar only makes sense in deployed environments that have a real,
    non-interactive cloud credential (a Managed Identity on Azure compute).
    Short-circuiting here means the chat tool still returns its report DATA to
    the LLM; only the optional download link is skipped.

    Enabled only when BOTH hold:
      * environment is not local/dev/test, and
      * AZURE_STORAGE_ACCOUNT_URL is set. The credential itself is Managed
        Identity, ambient on the compute resource, so there is no key path to
        check; the account URL is the only thing that must be explicit.

    An operator can force-enable with GCS_EXPORT_ENABLED=1 / disable with =0
    (name kept for backward compatibility — the prod deploy manifest already
    sets it).
    """
    forced = os.getenv("GCS_EXPORT_ENABLED")
    if forced is not None:
        return forced.strip().lower() in ("1", "true", "yes", "on")

    env = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "").strip().lower()
    if env in ("local", "dev", "development", "test", "testing", ""):
        return False

    if not AZURE_STORAGE_ACCOUNT_URL:
        logger.warning(
            "CSV export disabled: AZURE_STORAGE_ACCOUNT_URL not set "
            "(environment=%s). Returning row data without a download link.",
            env or "<unset>",
        )
        return False
    return True


def generate_csv_export_gcs(data: Any, filename_prefix: str = "export") -> Dict[str, str]:
    """
    Generate a CSV file and upload it via the active cloud-storage backend.
    Files auto-expire after CSV_EXPIRATION_HOURS (default: 24 hours).
    
    Args:
        data: Data to export (list of dicts or dict with data key)
        filename_prefix: Prefix for the filename
        
    Returns:
        Dict with public_url, filename, and expiration info
    """
    # Extract / validate / flatten rows up-front (pure, no network). This is the
    # data we hand back regardless of whether the upload succeeds.
    rows = extract_data_rows(data)
    if not rows or (isinstance(rows, dict) and 'error' in rows):
        return rows

    # Build the CSV in memory (still no network).
    csv_buffer = io.StringIO()
    # Header = union of keys across ALL rows (first-seen order), not just
    # rows[0]. extract_data_rows flattens each row independently, so a later
    # row can carry an optional key the first row lacks; with the default
    # extrasaction='raise' that raised 'fields not in fieldnames' here —
    # before the upload try/except — and 500'd the CSV export tool.
    # extrasaction='ignore' + restval='' are belt-and-braces.
    headers = list(dict.fromkeys(k for row in rows for k in row.keys()))
    writer = csv.DictWriter(csv_buffer, fieldnames=headers, extrasaction="ignore", restval="")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    csv_content = csv_buffer.getvalue()

    file_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{filename_prefix}_{timestamp}_{file_id}.csv"

    # If cloud storage isn't configured for this environment, skip the upload.
    # The caller only emits a download link when a
    # `download_url` is present, so omitting it degrades gracefully: the LLM
    # still gets the full tool result, just without a CSV link.
    if not _gcs_export_enabled():
        logger.info(
            "CSV export: cloud storage disabled for this environment — returning "
            "%d rows without upload (no download link).",
            len(rows),
        )
        return {
            "filename": filename,
            "rows": len(rows),
            "size_bytes": len(csv_content),
            "storage": "skipped",
            "note": "CSV upload skipped (cloud storage not configured in this environment).",
        }

    # Attempt the upload under a hard wall-clock timeout so a stuck credential
    # fetch can never block the request beyond GCS_UPLOAD_TIMEOUT_SECONDS.
    # NOTE: we deliberately do NOT use the executor as a context manager — its
    # __exit__ calls shutdown(wait=True), which would block on a hung worker and
    # defeat the very timeout we rely on. shutdown(wait=False) lets the daemon
    # worker die with the process if it's truly wedged.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_upload_csv_via_backend, filename, csv_content, len(rows))
        result = future.result(timeout=GCS_UPLOAD_TIMEOUT_SECONDS)
        pool.shutdown(wait=False)
        return result
    except concurrent.futures.TimeoutError:
        pool.shutdown(wait=False)
        logger.error(
            "CSV export: upload exceeded %ss timeout — abandoning upload, "
            "returning %d rows without a download link.",
            GCS_UPLOAD_TIMEOUT_SECONDS,
            len(rows),
        )
        return {
            "filename": filename,
            "rows": len(rows),
            "size_bytes": len(csv_content),
            "storage": "timeout",
            "note": "CSV upload timed out; report data returned without a link.",
        }
    except Exception as e:
        pool.shutdown(wait=False)
        logger.error(f"Error uploading CSV to cloud storage: {e}")
        return {"error": str(e)}


def _upload_csv_via_backend(filename: str, csv_content: str, row_count: int) -> Dict[str, Any]:
    """Blocking upload via the active backend, run on a worker thread under a
    timeout by the caller (see generate_csv_export_gcs).

    Renamed from `_upload_csv_to_gcs` (private, not referenced by name in any
    test — safe to rename). What lives here is the backend-agnostic envelope —
    object path, metadata, expiry math, response shape — with all
    backend-specific behaviour behind `shared/storage_backends/`.
    """
    backend = _get_backend()
    object_path = f"csv_exports/{filename}"

    expiration_time = datetime.now() + timedelta(hours=CSV_EXPIRATION_HOURS)
    metadata = {
        'expires_at': expiration_time.isoformat(),
        'created_by': 'ai-engine',
    }

    download_url = backend.upload_csv(object_path, csv_content, metadata=metadata)

    return {
        "filename": filename,
        "rows": row_count,
        "download_url": download_url,
        "size_bytes": len(csv_content),
        "expires_at": expiration_time.isoformat(),
        "storage": backend.name,
    }


# ---------------------------------------------------------------------------
# Generic JSON object helpers
#
# Deliberately separate from the CSV export path above: no download-link
# semantics, no 24h auto-expire, no public-read. These are for durable
# internal artifacts (e.g. the precompiled prod config bundle in
# runtime/prod_bundle.py) that the service reads back at boot. Same lazy
# SDK access pattern as `_upload_csv_via_backend` so a missing/broken storage
# install never breaks import of this module.
# ---------------------------------------------------------------------------


def upload_json(path: str, obj: Any) -> str:
    """Serialise `obj` to JSON and upload it via the active backend.

    Returns the backend-native URI,
    ``azure-blob://<AZURE_STORAGE_CONTAINER>/<path>``. Raises on any backend/serialisation
    failure — the caller decides whether to swallow (e.g. a promote-time bundle
    push must never fail the promote, so it wraps this in try/except).
    """
    backend = _get_backend()
    content = json.dumps(obj, ensure_ascii=False, default=str)
    return backend.upload_json(path, content)


def download_json(path: str) -> Optional[Dict[str, Any]]:
    """Download + parse the JSON object at `path` via the active backend.

    Returns ``None`` if the object does not exist or on ANY backend / parse
    error — the caller (e.g. the prod boot loader) decides whether a miss
    is fatal. Never raises.
    """
    try:
        backend = _get_backend()
        text = backend.download_text(path)
        if text is None:
            return None
        return json.loads(text)
    except Exception as exc:
        logger.warning("download_json: failed for %s (%s)", path, exc)
        return None


def extract_data_rows(data: Any) -> Any:
    """Extract rows from various data structures"""
    try:
        rows = None
        data_source_key = None
        
        if isinstance(data, dict):
            # Try common keys for data arrays - prioritize detailed/granular arrays over aggregates
            for key in [
                'clients_aggregate_report',
                'services_report',
                'staff_list_reports',
                'staff_insights_daily',
                'staff',
                'bookings',
                'invoices',
                'products',
                'daily_breakdown',
                'daily_clients_report',
                'data',
                'items',
                'results',
                'services',
                'clients',
                'categories_report'
            ]:
                if key in data and isinstance(data[key], list) and len(data[key]) > 0:
                    rows = data[key]
                    data_source_key = key
                    logger.info(f"CSV export: Found {len(rows)} rows in key '{key}'")
                    break
            
            # If still no rows, check if the dict itself has row-like structure
            if not rows and len(data) > 0 and not any(k in data for k in ['_metadata', '_csv_export', '_note']):
                rows = [data]
                data_source_key = 'single_row'
                logger.info(f"CSV export: Converting single dict to list")
        elif isinstance(data, list) and len(data) > 0:
            rows = data
            data_source_key = 'root_array'
            logger.info(f"CSV export: Using list directly with {len(rows)} rows")
        
        if not rows:
            logger.warning(f"CSV export: No suitable data found")
            return {"error": "No suitable data array found for CSV export"}
        
        if len(rows) == 0:
            return {"error": "Data array is empty"}
        
        # Validate that rows is actually a list of dicts
        if not isinstance(rows, list):
            logger.error(f"CSV export: rows is not a list, it's {type(rows)}")
            return {"error": f"Expected list of rows, got {type(rows)}"}
        
        if not isinstance(rows[0], dict):
            logger.error(f"CSV export: First row is not a dict, it's {type(rows[0])}")
            return {"error": f"Expected dict rows, got {type(rows[0])}"}
        
        logger.info(f"CSV export: Validated {len(rows)} rows, first row has {len(rows[0])} columns")
        
        # Flatten nested objects for CSV
        flattened_rows = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                logger.warning(f"CSV export: Row {idx} is not a dict, skipping")
                continue
                
            flattened_row = {}
            for key, value in row.items():
                if isinstance(value, (dict, list)):
                    flattened_row[key] = json.dumps(value, ensure_ascii=False)
                elif value is None:
                    flattened_row[key] = ''
                else:
                    flattened_row[key] = value
            flattened_rows.append(flattened_row)
        
        logger.info(f"CSV export: Flattened {len(flattened_rows)} rows")
        return flattened_rows
        
    except Exception as e:
        logger.error(f"CSV extraction error: {e}")
        return {"error": str(e)}

