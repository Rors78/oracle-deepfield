"""M2 gate: series-equality — both engines see the identical closed series.

Skips if the DB hasn't been backfilled (M1). Reviewer #5: this proves the
monkeypatch strip removed exactly the dummy bar, so M3's diff is pure logic.
"""
import os

import pytest

from deepfield import config
from deepfield import parity


@pytest.mark.skipif(not os.path.exists(config.DB_PATH), reason="no DB (run M1 backfill)")
def test_series_equality_all_pairs():
    report = parity.run_series_equality()
    assert len(report) == len(config.PAIRS) * 2   # N pairs x 2 intervals (daily+weekly)
    for row in report:
        assert row["ok"], row
        if "reason" in row:
            # vacuous row: legitimate ONLY when the DB truly has <20 closed
            # candles (young listing) — never a mask for a backfill hole
            assert row["db_closed"] < 20, row
        else:
            assert row["v44_len"] == row["db_closed"]
            assert row["last_ts_match"]
