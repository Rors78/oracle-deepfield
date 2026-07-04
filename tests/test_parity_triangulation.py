"""M3 gate as a regression guard: Leg A 105/105, Leg B zero unattributed.

Skips without a backfilled DB (M1). Keeps the parity gate enforced in CI so a
future engine edit that breaks faithfulness or introduces an unattributed diff
fails loudly.
"""
import os

import pytest

from deepfield import config
from deepfield import parity


@pytest.mark.skipif(not os.path.exists(config.DB_PATH), reason="no DB (run M1 backfill)")
def test_M3_triangulation_gate():
    t = parity.triangulate()
    # Leg A: COMPAT reproduces v4.4 at every slot.
    assert t["legA_match"] == t["legA_total"] == 105, t["legA_diffs"]
    # Leg B: every FULL-vs-COMPAT diff is attributable to an F-item.
    assert t["unattributed"] == [], t["unattributed"]
