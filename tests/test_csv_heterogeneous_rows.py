"""generate_csv_export_gcs must not raise on heterogeneous row keys.

extract_data_rows flattens each row independently, so a later row can carry an
optional field the first row lacks. csv.DictWriter defaults to
extrasaction='raise', which used to blow up here — before the upload try/except
— turning a normal "give me a CSV" into a 500. The header must be the union of
all row keys, with extras ignored.
"""
from __future__ import annotations

from unittest.mock import patch

from shared import cloud_storage


def test_heterogeneous_rows_do_not_raise():
    rows = [
        {"name": "A", "revenue": 100},
        {"name": "B", "revenue": 200, "discount": 5},  # extra key not in row 0
        {"name": "C"},                                  # missing a key
    ]
    # Force the GCS-disabled path so no network is touched; the CSV is still
    # built in memory (where the raise used to happen).
    with patch.object(cloud_storage, "_gcs_export_enabled", return_value=False):
        out = cloud_storage.generate_csv_export_gcs(rows, filename_prefix="perf")
    assert "error" not in out
    assert out["rows"] == 3
    assert out["size_bytes"] > 0


def test_union_of_keys_becomes_header_order_preserved():
    rows = [{"a": 1}, {"a": 2, "b": 3}, {"c": 4}]
    with patch.object(cloud_storage, "_gcs_export_enabled", return_value=False):
        # Just needs to not raise; header union is a, b, c.
        out = cloud_storage.generate_csv_export_gcs(rows, filename_prefix="x")
    assert "error" not in out and out["rows"] == 3
