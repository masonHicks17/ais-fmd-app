"""
natural_key is a persisted contract: every live row was keyed with it.

If this hash changes, re-uploading an already-imported statement inserts every
row again. Changing it needs a key backfill of the live table first.
"""

import pandas as pd

from ais_fmd.domain.dedupe import natural_key, split_new_and_duplicate

ROW = {"transaction_date": "2026-01-22", "amount": -4.5, "details": "COFFEE", "account": "Wells Fargo"}


def test_natural_key_is_stable():
    assert natural_key(ROW) == "4c96bcd5400b3c5b9cdf6c3a70a93884a751c201"


def test_a_timestamp_date_keys_like_its_string():
    assert natural_key({**ROW, "transaction_date": pd.Timestamp("2026-01-22")}) == natural_key(ROW)


def test_a_stored_row_read_back_as_timestamp_is_a_duplicate():
    stored = {**ROW, "transaction_date": pd.Timestamp("2026-01-22")}
    result = split_new_and_duplicate([ROW], [stored])
    assert len(result.duplicates) == 1 and not result.new
