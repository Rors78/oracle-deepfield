"""Backtest: sig7's 52-week low from weekly CLOSES (live) vs weekly LOWS.

    ./venv/bin/python -m deepfield.backtest_sig7

sig7 claims "price is within 20% of the 52-week low" but takes that low from the
weekly CLOSE series, never the weekly LOW series (`wl` is loaded and passed to the
engine, but only sig6 reads it). A close-based low sits above the true low, so
price looks nearer the bottom than it is and the signal fires more readily than
its own name implies — for 8 of 21 live pairs as of 2026-07-19.

The change is local to sig7, so this tests it standalone across the FULL weekly
history rather than being capped at the ~2y of daily data a whole-scorecard walk
would need (Kraken's 720-row fetch cap sets depth: 1d ~2y, 1w ~13.8y).

METHOD. The obvious metric — forward returns when fired vs not fired — is
CONFOUNDED and is deliberately not used: "near the 52w low" weeks are bear weeks
and "far from it" weeks are bull weeks, so it mostly measures regime, and both
variants inherit that bias equally so it cannot rank them either.

`lows` fires as a strict subset of `closes` (verified below, 0 violations), so the
change only ever REMOVES weeks. That makes the decision-relevant question narrow
and confound-matched, since every week compared already fires the live definition:

    Among weeks the CURRENT definition fires on, do the weeks the change would
    REMOVE have worse forward returns than the ones it KEEPS?

STATS. Observations overlap (52w windows, h-week forwards) and crypto pairs are
highly correlated, so pooled n is not independent and a t-test on it would be
fiction. Significance is a per-pair sign test — robust to within-pair
autocorrelation, still NOT robust to cross-pair correlation, so it overstates
confidence in a single-beta asset class. Treat p as an upper bound on certainty.

RESULT (2026-07-19): removed weeks were worse at 4w/13w/26w — 13w medians -9.85%
removed vs +0.22% kept — but the per-pair sign test never cleared p=0.143, the
effect REVERSED at 52w, and the change kills sig7 outright for SUI (one wick sits
76.9% below the lowest weekly close, poisoning the window for a year). Winsorized
middle grounds keep every pair alive but surrender the gain. Verdict: NOT adopted.
"""
import math
import statistics as st

from . import config, store

HORIZONS = (4, 13, 26, 52)
THRESH = 0.20
# k-th lowest weekly low instead of the absolute min — a wick-robust middle ground.
WINSOR = {"closes": None, "lows(min)": 0, "lows(2nd)": 1, "lows(3rd)": 2, "lows(5th)": 4}


def _series(conn, pair):
    rows = conn.execute(
        "SELECT c, l FROM candles WHERE pair=? AND interval=10080 AND closed=1 ORDER BY ts",
        (pair,)).fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def _observations(wc, wl):
    """(fires_by_variant, {horizon: forward_return}) per weekly bar. No lookahead:
    every low window ends at the evaluated bar."""
    n = len(wc)
    for t in range(51, n):
        px = wc[t]
        if not px or px <= 0:
            continue
        win_c = sorted(v for v in wc[t - 51:t + 1] if v and v > 0)
        win_l = sorted(v for v in wl[t - 51:t + 1] if v and v > 0)
        if len(win_c) < 52 or len(win_l) < 10:
            continue
        fwd = {h: (wc[t + h] - px) / px
               for h in HORIZONS if t + h < n and wc[t + h] and wc[t + h] > 0}
        if not fwd:
            continue
        fires = {}
        for name, k in WINSOR.items():
            lo = win_c[0] if k is None else win_l[min(k, len(win_l) - 1)]
            fires[name] = (px - lo) / lo <= THRESH
        yield fires, fwd


def _sign_test(better, worse):
    n = better + worse
    if not n:
        return None
    k = max(better, worse)
    return min(sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n * 2, 1.0)


def main():
    conn = store.connect(config.DB_PATH)
    live = sorted({p["ws"] for p in config.PAIRS})
    per_pair = {}
    for p in live:
        wc, wl = _series(conn, p)
        obs = list(_observations(wc, wl))
        if len(obs) >= 60:
            per_pair[p] = obs
    conn.close()
    pooled = [o for obs in per_pair.values() for o in obs]
    if not pooled:
        print("no data — run the backfill first")
        return

    violations = sum(1 for f, _ in pooled if f["lows(min)"] and not f["closes"])
    print(f"{len(per_pair)} pairs, {len(pooled)} pair-weeks (overlapping — see docstring)")
    print(f"'lows' a strict subset of 'closes'? violations: {violations}\n")

    print("=== the decision: weeks the change KEEPS vs weeks it REMOVES ===")
    print("    (every week below already fires the CURRENT definition)")
    print(f"{'horizon':>8}{'KEPT mean':>12}{'REM mean':>11}{'KEPT med':>11}{'REM med':>10}"
          f"{'sign test':>12}")
    for h in HORIZONS:
        kept = [f_[1][h] for f_ in pooled if f_[0]["closes"] and f_[0]["lows(min)"] and h in f_[1]]
        rem = [f_[1][h] for f_ in pooled if f_[0]["closes"] and not f_[0]["lows(min)"] and h in f_[1]]
        if len(kept) < 20 or len(rem) < 20:
            continue
        b = w = 0
        for obs in per_pair.values():
            kk = [o[1][h] for o in obs if o[0]["closes"] and o[0]["lows(min)"] and h in o[1]]
            rr = [o[1][h] for o in obs if o[0]["closes"] and not o[0]["lows(min)"] and h in o[1]]
            if len(kk) >= 10 and len(rr) >= 10:
                b, w = (b + 1, w) if st.mean(kk) > st.mean(rr) else (b, w + 1)
        pv = _sign_test(b, w)
        print(f"{h:>6}w {st.mean(kept):>+11.2%} {st.mean(rem):>+10.2%} "
              f"{st.median(kept):>+10.2%} {st.median(rem):>+9.2%}"
              f"{f'{b}/{b + w} p={pv:.3f}':>15}")
    print("  KEPT > REMOVED => the change drops bad entries")

    print("\n=== cost: fire rate, and does the signal survive on every pair? ===")
    print(f"{'variant':<11}{'fire%':>7}   dead pairs (signal never fires)")
    for name in WINSOR:
        rate = sum(1 for f, _ in pooled if f[name]) / len(pooled)
        dead = [p.split("/")[0] for p, obs in per_pair.items()
                if not any(o[0][name] for o in obs)]
        print(f"{name:<11}{rate:>6.1%}   {', '.join(dead) if dead else '-'}")


if __name__ == "__main__":
    main()
