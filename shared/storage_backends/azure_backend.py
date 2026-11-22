"""Azure Blob Storage backend.

Authenticates with `azure.identity.DefaultAzureCredential` — deliberately NOT a
connection string or account key. On Azure compute with a Managed Identity
assigned (Container Apps, VM, App Service), `DefaultAzureCredential` picks it up
with no configuration beyond `AZURE_STORAGE_ACCOUNT_URL`; no secret is ever
minted, stored, or rotated for this path. Off-Azure it falls back through the
Azure CLI credential (`az login`) and friends — useful for local dev, never used
in prod.

SDK imports are lazy inside each method so a missing/broken azure install can't
break `import shared.cloud_storage` (nor the fast no-op dev path where the CSV
export is disabled outright).

--- Expiry semantics (read before changing CSV_EXPIRATION_HOURS) ---
This backend defaults (`AZURE_BLOB_PUBLIC_CONTAINER` unset/false) to a
user-delegation SAS whose expiry is a REAL, enforced deadline at
`now + CSV_EXPIRATION_HOURS`: the URL 403s at that instant even though the blob
may physically survive until a storage-account lifecycle policy removes it. This
is the single biggest caller-observable difference from the GCS path.

`AZURE_BLOB_PUBLIC_CONTAINER=true` switches to a bare, non-expiring blob URL
but that requires the storage account to have `allowBlobPublicAccess=true` and
the container's public access level set to "Blob" out-of-band. There is no
per-object ACL, and many subscriptions disable anonymous container access by
policy — so treat it as an opt-in for less locked-down subscriptions, not the
default expectation.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _public_container_enabled() -> bool:
    # Read fresh (not frozen at construction) so ops/tests can flip this without
    # reloading the module — mirrors CLOUD_STORAGE_BACKEND's own env lookup.
    return (os.getenv("AZURE_BLOB_PUBLIC_CONTAINER", "false").strip().lower()
            in ("1", "true", "yes", "on"))


class AzureBlobBackend:
    name = "azure_blob"

    def __init__(self, account_url: str, container_name: str, expiration_hours: int):
        self._account_url = account_url
        self._container_name = container_name
        self._expiration_hours = expiration_hours

    def _service_client(self) -> Any:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        # Fresh credential + client per call — mirrors the pre-refactor GCS code,
        # which never cached storage.Client() either. DefaultAzureCredential's
        # constructor does no network I/O; the token fetch happens lazily inside
        # the first SDK call, so this stays cheap.
        return BlobServiceClient(self._account_url, credential=DefaultAzureCredential())

    def _blob_client(self, path: str) -> Any:
        service = self._service_client()
        return service.get_blob_client(container=self._container_name, blob=path)

    def upload_csv(self, path: str, content: str, *, metadata: Dict[str, str]) -> str:
        from azure.storage.blob import ContentSettings

        blob = self._blob_client(path)
        blob.upload_blob(
            content,
            overwrite=True,
            content_settings=ContentSettings(content_type="text/csv"),
            metadata=metadata,
        )

        if _public_container_enabled():
            download_url = blob.url
        else:
            download_url = self._sas_url(blob, path)

        logger.info(
            "CSV uploaded to Azure Blob Storage: %s (%s bytes, %s) -> %s",
            path.rsplit("/", 1)[-1], len(content),
            "public container" if _public_container_enabled()
            else f"SAS expires in {self._expiration_hours}h",
            download_url,
        )
        return download_url

    def upload_json(self, path: str, content: str) -> str:
        from azure.storage.blob import ContentSettings

        blob = self._blob_client(path)
        blob.upload_blob(
            content,
            overwrite=True,
            content_settings=ContentSettings(content_type="application/json"),
        )
        logger.info("uploaded JSON object to azure-blob://%s/%s", self._container_name, path)
        return f"azure-blob://{self._container_name}/{path}"

    def download_text(self, path: str) -> Optional[str]:
        from azure.core.exceptions import ResourceNotFoundError

        blob = self._blob_client(path)
        try:
            downloader = blob.download_blob(encoding="utf-8")
        except ResourceNotFoundError:
            return None
        return downloader.readall()

    def _sas_url(self, blob_client: Any, path: str) -> str:
        """Read-only user-delegation SAS expiring at now + CSV_EXPIRATION_HOURS.

        Requires the caller's Managed Identity to hold
        `...generateUserDelegationKey/action` — the built-in "Storage Blob
        Delegator" role, or the broader "Storage Blob Data Contributor". Not the
        account key, so this stays zero-secret end to end.
        """
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        service = self._service_client()
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(hours=self._expiration_hours)
        user_delegation_key = service.get_user_delegation_key(
            key_start_time=now, key_expiry_time=expiry,
        )
        sas_token = generate_blob_sas(
            account_name=service.account_name,
            container_name=self._container_name,
            blob_name=path,
            user_delegation_key=user_delegation_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )
        return f"{blob_client.url}?{sas_token}"
