"""Blob-store backends for `shared.cloud_storage`.

Nothing outside `shared/` should import from this package — callers go
through `shared.cloud_storage`'s public functions, which dispatch to the
backend selected by `CLOUD_STORAGE_BACKEND`.
"""
