"""Backend interface for `shared.cloud_storage`.

`azure_backend.AzureBlobBackend` implements this `Protocol`. It exists so
`cloud_storage.py`'s public functions can stay short, backend-agnostic
dispatchers — and so a second backend can be added later without touching them.
(A `gcs_backend.GCSBackend` sibling existed until 2026-08-16; it was removed
with the rest of the dead GCP infrastructure.)

Backends are constructed fresh per call, and build their underlying SDK client
fresh per call too. Keep it that way: don't add a process-wide cached client
without first checking the SDK's fork-safety story — container runtimes
fork/re-exec workers in ways that make client reuse across threads/processes
risky without real testing.
"""
from __future__ import annotations

from typing import Dict, Optional, Protocol


class StorageBackend(Protocol):
    """Everything `shared.cloud_storage` needs from a blob store."""

    #: Short backend id echoed into the `storage` field of
    #: `generate_csv_export_gcs`'s return dict ("azure_blob").
    name: str

    def upload_csv(self, path: str, content: str, *, metadata: Dict[str, str]) -> str:
        """Upload `content` as `text/csv` at `path`; return a URL an end user
        can download it from.

        The backend does whatever makes the object fetchable at that URL
        (Azure: a SAS token, or the bare blob URL when the container is
        configured for anonymous read). `metadata` is best-effort descriptive
        tagging (e.g. `expires_at`) and is NOT expiry enforcement — see
        `cloud_storage`'s module docstring.
        """
        ...

    def upload_json(self, path: str, content: str) -> str:
        """Upload already-serialised JSON `content` as `application/json` at
        `path`. Returns the backend-native URI
        (`azure-blob://<container>/<path>`). Must RAISE on failure —
        `cloud_storage.upload_json` callers rely on failing loudly.
        """
        ...

    def download_text(self, path: str) -> Optional[str]:
        """Return the object's decoded text, or `None` if it does not exist.

        May raise on genuine backend errors (auth, network, permissions):
        `cloud_storage.download_json` catches broadly and treats any exception
        as a miss, so backends need not pre-catch anything beyond not-found.
        """
        ...
