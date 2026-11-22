"""Backend-selection + backend-implementation tests for `shared.cloud_storage`.

Azure Blob is the ONLY backend since 2026-08-16 — the GCS backend and
`google-cloud-storage` were removed with the rest of the dead GCP
infrastructure, and the default flipped from `"gcs"` to `"azure_blob"` (the old
default silently sent the self-hosted demo stacks, whose compose files set no
`CLOUD_STORAGE_BACKEND`, at a GCS backend with no credentials on Azure compute).

Three groups:
  1. Backend selection (`_backend_name` / `_get_backend`) and the
     `_gcs_export_enabled` gate.
  2. Backend-agnostic orchestration (`generate_csv_export_gcs`, `upload_json`,
     `download_json`) via a fake backend, proving those functions don't hardcode
     any one backend.
  3. Azure backend behaviour against the real SDK surface (monkeypatched, no
     network), proving it authenticates via DefaultAzureCredential with a real
     SAS expiry.
"""
from __future__ import annotations

import sys
from datetime import timedelta

import pytest

from shared import cloud_storage
from shared.storage_backends.azure_backend import AzureBlobBackend


# ---------------------------------------------------------------------------
# 0. The SDK is not force-imported at module import.
# ---------------------------------------------------------------------------
def test_importing_cloud_storage_does_not_eagerly_import_the_sdk():
    for mod in list(sys.modules):
        if mod in ("azure.storage.blob", "azure.identity") or mod.startswith(
            ("azure.storage.blob.", "azure.identity.")
        ):
            del sys.modules[mod]

    import importlib

    importlib.reload(cloud_storage)

    assert "azure.storage.blob" not in sys.modules
    assert "azure.identity" not in sys.modules


def test_gcs_backend_module_is_gone():
    """The GCS backend was deleted, not just deselected — `google-cloud-storage`
    is no longer a dependency, so a module that lazily imported it would be a
    trap that only fires on the CSV path in whichever stack still set
    CLOUD_STORAGE_BACKEND=gcs."""
    with pytest.raises(ImportError):
        import shared.storage_backends.gcs_backend  # noqa: F401


# ---------------------------------------------------------------------------
# 1. Backend selection + enablement gate
# ---------------------------------------------------------------------------
def test_get_backend_defaults_to_azure_blob_when_unset(monkeypatch):
    """The default is azure_blob, NOT gcs. This pins the 2026-08-16 flip: the
    self-hosted demo docker-compose files set no CLOUD_STORAGE_BACKEND at all, so
    whatever this resolves to is what those standalone stacks get.

    Asserts `_backend_name()` and not just the returned object: every value now
    yields an AzureBlobBackend, so an isinstance check alone would still pass if
    the default were reverted to "gcs". The resolved NAME is the only thing that
    discriminates."""
    monkeypatch.delenv("CLOUD_STORAGE_BACKEND", raising=False)
    assert cloud_storage._backend_name() == "azure_blob"
    backend = cloud_storage._get_backend()
    assert isinstance(backend, AzureBlobBackend)
    assert backend.name == "azure_blob"


def test_unset_backend_does_not_warn(monkeypatch, caplog):
    """The default must be a supported value. If someone reverted DEFAULT_BACKEND
    to "gcs", `_get_backend` would emit its "not supported" warning on a stack
    that configured nothing — this catches that even though the returned backend
    would be identical."""
    monkeypatch.delenv("CLOUD_STORAGE_BACKEND", raising=False)
    with caplog.at_level("WARNING"):
        cloud_storage._get_backend()
    assert "not supported" not in caplog.text


@pytest.mark.parametrize("value", ["azure_blob", "azure", "azureblob"])
def test_get_backend_selects_azure_blob(value, monkeypatch):
    monkeypatch.setenv("CLOUD_STORAGE_BACKEND", value)
    backend = cloud_storage._get_backend()
    assert isinstance(backend, AzureBlobBackend)
    assert backend.name == "azure_blob"


@pytest.mark.parametrize("value", ["s3", "gcs"])
def test_get_backend_unsupported_value_warns_and_uses_azure(value, monkeypatch, caplog):
    """A retired/unknown value must degrade to Azure with a WARNING, not raise
    and not silently import a removed SDK."""
    monkeypatch.setenv("CLOUD_STORAGE_BACKEND", value)
    with caplog.at_level("WARNING"):
        backend = cloud_storage._get_backend()
    assert isinstance(backend, AzureBlobBackend)
    assert "not supported" in caplog.text


def test_gate_forced_on_short_circuits_regardless_of_backend(monkeypatch):
    monkeypatch.setenv("GCS_EXPORT_ENABLED", "true")
    monkeypatch.setenv("CLOUD_STORAGE_BACKEND", "azure_blob")
    monkeypatch.setattr(cloud_storage, "AZURE_STORAGE_ACCOUNT_URL", "")
    # Forced-on wins even with no Azure account URL — matches the pre-refactor
    # behaviour where GCS_EXPORT_ENABLED=true short-circuited past the
    # GOOGLE_APPLICATION_CREDENTIALS check.
    assert cloud_storage._gcs_export_enabled() is True


def test_gate_requires_azure_account_url_when_not_forced(monkeypatch):
    monkeypatch.delenv("GCS_EXPORT_ENABLED", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("CLOUD_STORAGE_BACKEND", "azure_blob")
    monkeypatch.setattr(cloud_storage, "AZURE_STORAGE_ACCOUNT_URL", "")
    assert cloud_storage._gcs_export_enabled() is False


def test_gate_true_when_azure_account_url_set(monkeypatch):
    monkeypatch.delenv("GCS_EXPORT_ENABLED", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("CLOUD_STORAGE_BACKEND", "azure_blob")
    monkeypatch.setattr(cloud_storage, "AZURE_STORAGE_ACCOUNT_URL", "https://a.blob.core.windows.net")
    assert cloud_storage._gcs_export_enabled() is True


def test_gate_local_env_disables_regardless_of_backend(monkeypatch):
    monkeypatch.delenv("GCS_EXPORT_ENABLED", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("CLOUD_STORAGE_BACKEND", "azure_blob")
    assert cloud_storage._gcs_export_enabled() is False


def test_gate_requires_account_url_even_with_a_retired_backend_value(monkeypatch):
    """A stale CLOUD_STORAGE_BACKEND=gcs must not route around the Azure gate —
    the gate is now backend-independent and keys only on the account URL."""
    monkeypatch.delenv("GCS_EXPORT_ENABLED", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("CLOUD_STORAGE_BACKEND", "gcs")
    monkeypatch.setattr(cloud_storage, "AZURE_STORAGE_ACCOUNT_URL", "")
    assert cloud_storage._gcs_export_enabled() is False


def test_gate_ignores_google_application_credentials(monkeypatch):
    """GOOGLE_APPLICATION_CREDENTIALS used to enable the export on the GCS path.
    It is now inert — leaving it honoured would keep a GCP-only signal switching
    on an Azure upload."""
    monkeypatch.delenv("GCS_EXPORT_ENABLED", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setattr(cloud_storage, "AZURE_STORAGE_ACCOUNT_URL", "")
    assert cloud_storage._gcs_export_enabled() is False


# ---------------------------------------------------------------------------
# 2. Backend-agnostic orchestration
# ---------------------------------------------------------------------------
class _FakeBackend:
    def __init__(self, name: str):
        self.name = name
        self.uploaded_csv: list = []
        self._json_store: dict = {}

    def upload_csv(self, path, content, *, metadata):
        self.uploaded_csv.append((path, content, metadata))
        return f"https://fake.example/{path}"

    def upload_json(self, path, content):
        self._json_store[path] = content
        return f"fake://{path}"

    def download_text(self, path):
        return self._json_store.get(path)


class _RaisingFakeBackend(_FakeBackend):
    def download_text(self, path):
        raise RuntimeError("backend exploded")


@pytest.mark.parametrize("backend_name", ["azure_blob", "some_future_backend"])
def test_generate_csv_export_routes_through_active_backend(backend_name, monkeypatch):
    fake = _FakeBackend(backend_name)
    monkeypatch.setattr(cloud_storage, "_gcs_export_enabled", lambda: True)
    monkeypatch.setattr(cloud_storage, "_get_backend", lambda: fake)

    out = cloud_storage.generate_csv_export_gcs([{"a": 1}, {"a": 2}], filename_prefix="sales")

    assert out["storage"] == backend_name
    assert out["download_url"].startswith("https://fake.example/csv_exports/")
    assert out["rows"] == 2
    assert "expires_at" in out
    path, content, metadata = fake.uploaded_csv[0]
    assert path.startswith("csv_exports/sales_")
    assert "a" in content
    assert set(metadata) == {"expires_at", "created_by"}
    assert metadata["created_by"] == "ai-engine"


def test_generate_csv_export_skips_upload_when_disabled(monkeypatch):
    fake = _FakeBackend("azure_blob")
    monkeypatch.setattr(cloud_storage, "_gcs_export_enabled", lambda: False)
    monkeypatch.setattr(cloud_storage, "_get_backend", lambda: fake)

    out = cloud_storage.generate_csv_export_gcs([{"a": 1}], filename_prefix="x")

    assert out["storage"] == "skipped"
    assert "download_url" not in out
    assert fake.uploaded_csv == []


@pytest.mark.parametrize("backend_name", ["azure_blob", "some_future_backend"])
def test_upload_and_download_json_round_trip(backend_name, monkeypatch):
    fake = _FakeBackend(backend_name)
    monkeypatch.setattr(cloud_storage, "_get_backend", lambda: fake)

    uri = cloud_storage.upload_json("ai-agent/prod-config/latest.json", {"schema_version": 1})
    assert uri == "fake://ai-agent/prod-config/latest.json"
    assert cloud_storage.download_json("ai-agent/prod-config/latest.json") == {"schema_version": 1}


def test_download_json_never_raises_when_backend_raises(monkeypatch):
    monkeypatch.setattr(cloud_storage, "_get_backend", lambda: _RaisingFakeBackend("azure_blob"))
    assert cloud_storage.download_json("some/path.json") is None


def test_download_json_returns_none_on_miss(monkeypatch):
    monkeypatch.setattr(cloud_storage, "_get_backend", lambda: _FakeBackend("azure_blob"))
    assert cloud_storage.download_json("never/uploaded.json") is None


# ---------------------------------------------------------------------------
# 3. Azure backend — Managed Identity, enforced SAS expiry, no secrets
# ---------------------------------------------------------------------------
def test_azure_backend_uses_default_azure_credential_and_real_sas_expiry(monkeypatch):
    import azure.identity as azure_identity_sdk
    import azure.storage.blob as azure_blob_sdk

    captured = {}

    class _FakeCredential:
        pass

    class _FakeBlobClient:
        def __init__(self, url):
            self.url = url

        def upload_blob(self, content, overwrite=None, content_settings=None, metadata=None):
            captured["uploaded"] = (
                content,
                content_settings.content_type if content_settings else None,
                metadata,
            )

    class _FakeServiceClient:
        account_name = "examplestorage"

        def __init__(self, account_url, credential=None):
            captured["account_url"] = account_url
            captured["credential"] = credential

        def get_blob_client(self, container, blob):
            return _FakeBlobClient(
                f"https://examplestorage.blob.core.windows.net/{container}/{blob}"
            )

        def get_user_delegation_key(self, key_start_time, key_expiry_time):
            captured["udk_start"] = key_start_time
            captured["udk_expiry"] = key_expiry_time
            return "FAKE_UDK"

    def _fake_generate_blob_sas(**kwargs):
        captured["sas_kwargs"] = kwargs
        return "sv=x&sr=b&sig=y"

    monkeypatch.setattr(azure_identity_sdk, "DefaultAzureCredential", _FakeCredential)
    monkeypatch.setattr(azure_blob_sdk, "BlobServiceClient", _FakeServiceClient)
    monkeypatch.setattr(azure_blob_sdk, "generate_blob_sas", _fake_generate_blob_sas)
    monkeypatch.delenv("AZURE_BLOB_PUBLIC_CONTAINER", raising=False)

    backend = AzureBlobBackend(
        account_url="https://examplestorage.blob.core.windows.net",
        container_name="csv-exports",
        expiration_hours=24,
    )
    url = backend.upload_csv(
        "csv_exports/x.csv", "a,b\n1,2\n", metadata={"expires_at": "2026-07-27T00:00:00"},
    )

    assert url == (
        "https://examplestorage.blob.core.windows.net/csv-exports/"
        "csv_exports/x.csv?sv=x&sr=b&sig=y"
    )
    assert captured["uploaded"] == ("a,b\n1,2\n", "text/csv", {"expires_at": "2026-07-27T00:00:00"})
    # Managed Identity, never a connection string or account key.
    assert isinstance(captured["credential"], _FakeCredential)
    # SAS expiry is a real enforced deadline of now + CSV_EXPIRATION_HOURS.
    assert captured["udk_expiry"] - captured["udk_start"] == timedelta(hours=24)
    assert captured["sas_kwargs"]["expiry"] == captured["udk_expiry"]
    assert captured["sas_kwargs"]["permission"].read is True


def test_azure_backend_public_container_mode_returns_bare_url(monkeypatch):
    import azure.identity as azure_identity_sdk
    import azure.storage.blob as azure_blob_sdk

    class _FakeBlobClient:
        def __init__(self, url):
            self.url = url

        def upload_blob(self, *a, **k):
            pass

    class _FakeServiceClient:
        account_name = "acct"

        def __init__(self, account_url, credential=None):
            pass

        def get_blob_client(self, container, blob):
            return _FakeBlobClient(f"https://acct.blob.core.windows.net/{container}/{blob}")

    monkeypatch.setattr(azure_identity_sdk, "DefaultAzureCredential", object)
    monkeypatch.setattr(azure_blob_sdk, "BlobServiceClient", _FakeServiceClient)
    monkeypatch.setenv("AZURE_BLOB_PUBLIC_CONTAINER", "true")

    backend = AzureBlobBackend(
        account_url="https://acct.blob.core.windows.net", container_name="c", expiration_hours=24,
    )
    url = backend.upload_csv("csv_exports/x.csv", "a,b\n1,2\n", metadata={})

    assert url == "https://acct.blob.core.windows.net/c/csv_exports/x.csv"
    assert "?" not in url


def test_azure_backend_download_text_returns_none_when_missing(monkeypatch):
    import azure.storage.blob as azure_blob_sdk
    from azure.core.exceptions import ResourceNotFoundError

    class _FakeBlobClient:
        def download_blob(self, encoding=None):
            raise ResourceNotFoundError("The specified blob does not exist.")

    class _FakeServiceClient:
        def __init__(self, account_url, credential=None):
            pass

        def get_blob_client(self, container, blob):
            return _FakeBlobClient()

    monkeypatch.setattr(azure_blob_sdk, "BlobServiceClient", _FakeServiceClient)
    backend = AzureBlobBackend(
        account_url="https://acct.blob.core.windows.net", container_name="c", expiration_hours=24,
    )
    assert backend.download_text("missing.json") is None


def test_azure_backend_upload_json_returns_azure_blob_uri(monkeypatch):
    import azure.storage.blob as azure_blob_sdk

    class _FakeBlobClient:
        def upload_blob(self, content, overwrite=None, content_settings=None):
            self.uploaded = (content, content_settings.content_type)

    class _FakeServiceClient:
        def __init__(self, account_url, credential=None):
            pass

        def get_blob_client(self, container, blob):
            return _FakeBlobClient()

    monkeypatch.setattr(azure_blob_sdk, "BlobServiceClient", _FakeServiceClient)
    backend = AzureBlobBackend(
        account_url="https://acct.blob.core.windows.net", container_name="c", expiration_hours=24,
    )
    assert backend.upload_json("ai-agent/prod-config/latest.json", '{"a": 1}') == (
        "azure-blob://c/ai-agent/prod-config/latest.json"
    )
