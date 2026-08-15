"""Selftest for deepfield.prop_signal — fabricated signals against hand-calculated
values. Pure: patches journal/save_state to in-memory collectors so NO real file,
alert, or notification is touched (the suite once fired real notify-send; never
again). Run:  python3 prop_selftest.py
"""
import datetime as dt
import math
import sys

from deepfield import prop_signal as ps

FAIL = []


def check(name, cond, detail=""):
    tag = "ok " if cond else "FAIL"
    print(f"  [{tag}] {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


print("case 1 — passes every gate (entry 100, stop 98, tp 110, equity 5000)")
t = ps.evaluate("TEST1/USD", 100.0, 98.0, 110.0, 5000.0, 4850.0, 4850.0)
# hand math: per_unit_loss = 2 + .04 + .0392 + .033 = 2.1122
#            size = 40/2.1122 = 18.93760060, floored to 8dp
#            per_unit_gain = 10 − .04 − .044 − .033 = 9.883 ; rr = 9.883/2.1122 = 4.679
hand_size = math.floor(40.0 / 2.1122 * 1e8) / 1e8
check("passes", t["ok"], f"reasons={t['reasons']}")
check("size matches hand calc 18.9376006", abs(t["size_base"] - hand_size) < 1e-9
      and abs(hand_size - 18.9376006) < 1e-6, f"size={t['size_base']}")
check("rr matches hand calc 4.679", abs(t["rr"] - 4.679) < 1e-3, f"rr={t['rr']}")
check("risk within budget", t["risk_usd"] <= 40.0, f"risk={t['risk_usd']}")
check("leverage 2 (notional 1893.76, 25% lock rule)", t["leverage"] == 2)

print("case 2 — fails R:R (entry 100, stop 97, tp 102: DEEPFIELD-native shape)")
t2 = ps.evaluate("TEST2/USD", 100.0, 97.0, 102.0, 5000.0, 4850.0, 4850.0)
# hand: rr = (2−.04−.0408−.033)/(3+.04+.0388+.033) = 1.8862/3.1118 = 0.6061
check("rejected", not t2["ok"])
check("rejection names R:R", any("reward:risk" in r for r in t2["reasons"]), str(t2["reasons"]))
check("R:R is the ONLY failure", len(t2["reasons"]) == 1, str(t2["reasons"]))
check("computed rr 0.606", abs(t2["rr"] - 0.606) < 1e-3, f"rr={t2['rr']}")

print("case 3 — would breach MDD (case-1 geometry, equity 4880, MDD floor 4850)")
t3 = ps.evaluate("TEST3/USD", 100.0, 98.0, 110.0, 4880.0, 4850.0, 4753.0)
check("rejected", not t3["ok"])
check("rejection names MDD floor", any("MDD" in r for r in t3["reasons"]), str(t3["reasons"]))
check("MDL (4753) NOT named — 4840 clears it", not any("MDL" in r for r in t3["reasons"]))

print("MDL recompute math")
st = ps._default_state()
st["balance"] = 4900.0
now = dt.datetime(2026, 8, 15, 1, 0, tzinfo=dt.timezone.utc)
did = ps.maybe_reset_day(st, now)
check("recompute fired", did)
check("floor = 4900 x 0.97 = 4753.00", st["mdl_floor"] == 4753.0, f"floor={st['mdl_floor']}")
check("boundary is same-day 00:30 when now is 01:00",
      ps.last_reset_boundary(now) == dt.datetime(2026, 8, 15, 0, 30, tzinfo=dt.timezone.utc))
check("boundary is PREVIOUS day 00:30 when now is 00:10",
      ps.last_reset_boundary(dt.datetime(2026, 8, 15, 0, 10, tzinfo=dt.timezone.utc))
      == dt.datetime(2026, 8, 14, 0, 30, tzinfo=dt.timezone.utc))
check("no double recompute inside same day", not ps.maybe_reset_day(st, now))

print("fee + funding lifecycle (open 10 @ 100, close @ 110 after 9h)")
check("funding: 9h on $1000 = 2 charges x 0.055 = 0.11",
      abs(ps.funding_accrued(1000.0, 9.0) - 0.11) < 1e-9)
check("funding: 3h59m = 0 charges", ps.funding_accrued(1000.0, 3.983) == 0.0)
# patch I/O to in-memory so the CLI functions run without touching real files
_journaled = []
ps.journal = lambda rec, path=None: _journaled.append(rec) or rec
ps.save_state = lambda st, path=None: None
st2 = ps._default_state()
ps._cmd_open(st2, "TEST/USD", "buy", 100.0, 10.0, 98.0, 110.0)
check("open fee 0.40 debited at open", abs(st2["balance"] - 4999.6) < 1e-9,
      f"balance={st2['balance']}")
st2["positions"][0]["ts_open"] = (dt.datetime.now(dt.timezone.utc)
                                  - dt.timedelta(hours=9)).isoformat()
ps._cmd_close(st2, "TEST/USD", 110.0)
# hand: gross 100 − close fee 0.44 − funding 0.11 = +99.45 ; 4999.6 + 99.45 = 5099.05
check("close realizes gross-fee-funding: balance 5099.05",
      abs(st2["balance"] - 5099.05) < 1e-9, f"balance={st2['balance']}")
check("position removed", st2["positions"] == [])
check("both legs journaled", len(_journaled) == 2)

print()
if FAIL:
    print(f"SELFTEST FAILED — {len(FAIL)} assertion(s): {FAIL}")
    sys.exit(1)
print("SELFTEST GREEN — all assertions passed")
