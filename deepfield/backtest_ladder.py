"""Backtest the ladder step: flat LADDER_STEP_PCT vs ATR-scaled (k x ATR%).

    ./venv/bin/python -m deepfield.backtest_ladder

Simulates the real mechanic from executor._place_ladder_rung: an entry fills, the
next post-only rung rests one step below that fill, it fills, the next rests one
step below THAT — until a rung would land at/under the stop (the natural floor).
Every rung in a chain shares the SAME support stop, so if the stop is touched the
WHOLE chain exits together.

WHY IT WAS RUN. The universe spans 6x in volatility (TRX 1.8%/day vs ZEC 10.8%/day
ATR as of 2026-07-19), and a flat 1% rung sweeps 10.8 rungs on ZEC in one average
day versus 1.8 on TRX. That looked like the mechanism behind the "35 rungs stacked
in a ~4% band, coordinated stop-out, ~15% equity" incident, so ATR-scaling the step
looked obviously right.

RESULT (2026-07-19): the premise was WRONG and the change was NOT adopted. Note
the stop-out rate is IDENTICAL across every variant — spacing cannot affect whether
a shared stop is touched, so it never controlled coordinated-stop-out risk at all;
the rungs were always going to die together. The band width was a symptom of dense
deployment, not the cause. What the step actually controls is deployment DENSITY,
and denser won: flat 1% earned +3.82%/deployed-dollar at 30d vs +2.92% at k=1.00,
beating every ATR variant on 19-21 of 21 pairs at every horizon (p=0.000).

The real lever on joint exits is the SHARED STOP (per-rung stops), not the spacing.

SCOPE / BIAS, stated so the number is not over-read:
  - Return is per DEPLOYED dollar, so a variant cannot win by simply buying less.
  - It ignores leverage, margin and liquidation — and margin is the actual binding
    constraint (flat 1% consumes ~3.5x the margin per chain). A margin-aware test
    could still favour wider steps; this one cannot settle that.
  - Rungs cascade within a bar here; live fills are rate-limited by the ~8s poll,
    which BIASES this test toward the dense variant.
  - No signal gating (every bar is a candidate start), so ladder geometry is not
    contaminated by entry quality — which the sig7 work found to be beta anyway.
  - ~2y of daily history, mostly bear; stop-out rates run 59-81%. Mean is positive
    while median is negative: a right-skewed payoff whose edge lives in the tail.
"""
import math
import statistics as st

from . import config, store

HORIZONS = (14, 30, 60)
STOP_PCT = 0.08            # held fixed across variants so ONLY the step differs
ATR_N = 14
STEP_FLOOR, STEP_CEIL = 0.005, 0.06
VARIANTS = {"flat 1.0%": None, "ATR k=0.25": 0.25, "ATR k=0.50": 0.50,
            "ATR k=0.75": 0.75, "ATR k=1.00": 1.00, "ATR k=1.50": 1.50}


def _atr_pct(rows):
    """ATR% per index from bars up to that index only (no lookahead)."""
    out, trs = [None] * len(rows), []
    for i in range(1, len(rows)):
        h, l, pc = rows[i][1], rows[i][2], rows[i - 1][3]
        if not all((h, l, pc)) or pc <= 0:
            trs.append(None)
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)) / pc)
        w = [x for x in trs[-ATR_N:] if x is not None]
        if len(w) == ATR_N:
            out[i] = sum(w) / ATR_N
    return out


def _chain(rows, t0, step, horizon):
    """One laddered entry -> (pnl, deployed, stopped, n_fills)."""
    entry = rows[t0][3]
    if not entry or entry <= 0:
        return None
    stop = entry * (1 - STOP_PCT)
    fills = [entry]
    nxt = entry * (1 - step)
    end = min(t0 + horizon, len(rows) - 1)
    stopped = False
    for i in range(t0 + 1, end + 1):
        lo = rows[i][2]
        if not lo:
            continue
        if lo <= stop:                       # shared stop: whole chain exits together
            stopped = True
            break
        while nxt > stop and lo <= nxt:
            fills.append(nxt)
            nxt *= (1 - step)
    exit_px = stop if stopped else rows[end][3]
    if not exit_px:
        return None
    return sum(exit_px - f for f in fills), sum(fills), stopped, len(fills)


def main():
    conn = store.connect(config.DB_PATH)
    data = {}
    for p in sorted({q["ws"] for q in config.PAIRS}):
        rows = conn.execute(
            "SELECT o,h,l,c FROM candles WHERE pair=? AND interval=1440 AND closed=1 "
            "ORDER BY ts", (p,)).fetchall()
        if len(rows) >= 200:
            data[p] = (rows, _atr_pct(rows))
    conn.close()
    if not data:
        print("no data — run the backfill first")
        return

    for H in HORIZONS:
        print(f"\n{'='*72}\nHORIZON {H}d  (stop {STOP_PCT:.0%}, step clamped "
              f"{STEP_FLOOR:.1%}-{STEP_CEIL:.1%})\n{'='*72}")
        print(f"{'variant':<12}{'ret/deployed':>13}{'median':>10}{'stop-out%':>11}"
              f"{'avg rungs':>11}")
        by_pair = {}
        for name, k in VARIANTS.items():
            rets, stops, rungs, n, pp = [], 0, [], 0, {}
            for p, (rows, atrs) in data.items():
                local = []
                for t0 in range(ATR_N + 1, len(rows) - H - 1):
                    a = atrs[t0]
                    if a is None:
                        continue
                    step = min(max(config.LADDER_STEP_PCT if k is None else k * a,
                                   STEP_FLOOR), STEP_CEIL)
                    r = _chain(rows, t0, step, H)
                    if not r or r[1] <= 0:
                        continue
                    local.append(r[0] / r[1])
                    stops += r[2]
                    rungs.append(r[3])
                    n += 1
                if local:
                    pp[p] = st.mean(local)
                    rets.extend(local)
            by_pair[name] = pp
            if rets:
                print(f"{name:<12}{st.mean(rets):>+12.2%}{st.median(rets):>+10.2%}"
                      f"{stops / n:>10.1%}{st.mean(rungs):>11.1f}")

        base = by_pair["flat 1.0%"]
        print("\n  per-pair sign test vs flat 1%:")
        for name in VARIANTS:
            if name == "flat 1.0%":
                continue
            w = sum(1 for p, v in by_pair[name].items() if p in base and v > base[p])
            tot = sum(1 for p in by_pair[name] if p in base)
            if tot:
                k_ = max(w, tot - w)
                pv = min(sum(math.comb(tot, i) for i in range(k_, tot + 1)) / 2 ** tot * 2, 1.0)
                print(f"    {name:<12} beats flat on {w}/{tot} pairs   p={pv:.3f}")


if __name__ == "__main__":
    main()
