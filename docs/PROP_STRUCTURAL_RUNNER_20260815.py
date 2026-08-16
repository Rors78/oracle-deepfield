"""Level-structural events x prop geometry — Phase 1 study.
Implements docs/PROP_STRUCTURAL_PROTOCOL_20260815.md (commit 9a230fb) EXACTLY.
READ-ONLY on the live DB. No network.
"""
import sqlite3, sys, datetime, collections, statistics

WT = "/home/golden/oracle-deepfield/.claude/worktrees/deck-sota-upscale"
sys.path.insert(0, WT)
from deepfield import config

DB = "file:/home/golden/oracle-deepfield/deepfield.db?mode=ro"
DY = 86400
W0 = datetime.date(2026, 5, 17)
SPLIT = datetime.date(2026, 7, 21)
END = datetime.date(2026, 8, 16)
TP_RS = (2.5, 3.2, 4.0)
CENTER_R = 3.2
CAP_BARS = 14
MAX_STOP_PCT = 8.4

conn = sqlite3.connect(DB, uri=True)
conn.execute("PRAGMA query_only=1")


def wilder_atr(rows, i):
    """ATR14 in PRICE units at index i (rows: (ts,o,h,l,c), no lookahead)."""
    if i < 15:
        return None
    trs = []
    for j in range(1, i + 1):
        h, l, pc = rows[j][2], rows[j][3], rows[j - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:14]) / 14.0
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14.0
    return atr


events, rejected = [], collections.Counter()
btc_daily = {}

for p in config.PAIRS:
    sym = p["ws"]
    d = conn.execute("SELECT ts,o,h,l,c FROM candles WHERE pair=? AND interval=1440 "
                     "AND closed=1 ORDER BY ts", (sym,)).fetchall()
    if len(d) < 60:
        continue
    lows = [r[3] for r in d]
    closes = [r[4] for r in d]
    if sym == "BTC/USD":
        for r in d:
            btc_daily[datetime.datetime.fromtimestamp(r[0] + DY, datetime.timezone.utc).date()] = r[4]
    # confirmed swing lows: pivot at i (low[i] < low[i-1] and < low[i+1]),
    # KNOWN at close of i+1. swings_known_by[t] = list of (pivot_idx, level).
    swings = []          # (pivot_idx, level, known_at_idx)
    for i in range(1, len(d) - 1):
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            swings.append((i, lows[i], i + 1))

    for T in range(25, len(d)):
        date = datetime.datetime.fromtimestamp(d[T][0] + DY, datetime.timezone.utc).date()
        if date < W0 or date >= END:
            continue
        split = "train" if date < SPLIT else "validate"
        path = [(d[k][1], d[k][2], d[k][3], d[k][4]) for k in range(T + 1, min(T + 1 + CAP_BARS, len(d)))]
        known = [s for s in swings if s[2] <= T]

        def emit(kind, entry, stop):
            dist = (entry - stop) / entry * 100.0
            if stop >= entry:
                rejected[(kind, "degenerate")] += 1
                return
            if dist > MAX_STOP_PCT:
                rejected[(kind, "stop>8.4%")] += 1
                return
            events.append({"kind": kind, "sym": sym, "date": date, "split": split,
                           "entry": entry, "stop": stop, "dist": dist, "path": path})

        # A — level reclaim
        if known:
            atr = None
            for pv, L, _ka in reversed(known):
                if pv >= T:
                    continue
                breached = any(closes[j] < L for j in range(max(0, T - 10), T))
                if breached:
                    if closes[T] > L and closes[T - 1] <= L:
                        atr = wilder_atr(d, T)
                        if atr:
                            emit("A-reclaim", closes[T], L - 0.5 * atr)
                    break        # most recent qualifying swing low only
        # B — higher-low confirmation (new swing confirmed exactly at T)
        just = [s for s in known if s[2] == T]
        if just:
            pv2, S2, _ = just[-1]
            older = [s for s in known if s[2] < T and s[0] < pv2]
            if older and S2 > older[-1][1]:
                emit("B-higherlow", closes[T], S2)
        # C — failed breakdown
        if T >= 22:
            L20 = min(lows[T - 21: T - 1])
            if closes[T - 1] < L20 and closes[T] > L20:
                emit("C-failedbd", closes[T], min(lows[T - 1], lows[T]))

conn.close()


def resolve(ev, tp_r):
    e, s = ev["entry"], ev["stop"]
    tp = e + tp_r * (e - s)
    mae = 0.0
    for n, (o, h, l, c) in enumerate(ev["path"], 1):
        mae = max(mae, (e - l) / (e - s))
        if l <= s:
            return {"out": "stop", "r": -1.0, "t": n, "mae": mae}
        if h >= tp:
            return {"out": "tp", "r": tp_r, "t": n, "mae": mae}
    last_c = ev["path"][-1][3] if ev["path"] else e
    return {"out": "open", "r": (last_c - e) / (e - s), "t": len(ev["path"]), "mae": mae}


def agg(evs, tp_r):
    rs = [resolve(ev, tp_r) for ev in evs]
    n = len(rs)
    tps = [r for r in rs if r["out"] == "tp"]
    stops = [r for r in rs if r["out"] == "stop"]
    opens = [r for r in rs if r["out"] == "open"]
    resolved = len(tps) + len(stops)
    maes = sorted(r["mae"] for r in rs)
    ts = sorted(r["t"] for r in tps + stops)
    dists = sorted(ev["dist"] for ev in evs)
    return {"n": n, "tp": len(tps), "stop": len(stops), "open": len(opens),
            "hit": len(tps) / resolved if resolved else None,
            "be": 1.0 / (1.0 + tp_r),
            "exp_res": (len(tps) * tp_r - len(stops)) / resolved if resolved else None,
            "exp_all": sum(r["r"] for r in rs) / n if n else None,
            "mae_med": statistics.median(maes) if maes else None,
            "mae_p75": maes[int(0.75 * (len(maes) - 1))] if maes else None,
            "mae_max": maes[-1] if maes else None,
            "dist_med": statistics.median(dists) if dists else None,
            "t_med": statistics.median(ts) if ts else None}


def eff_n(evs):
    if not evs:
        return 0
    spans = sorted((ev["date"], ev["date"] + datetime.timedelta(days=CAP_BARS)) for ev in evs)
    clusters, cur_end = 1, spans[0][1]
    for st, en in spans[1:]:
        if st > cur_end:
            clusters += 1
            cur_end = en
        else:
            cur_end = max(cur_end, en)
    return clusters


def fmt(a):
    if a["n"] == 0:
        return "n=  0"
    h = f"{a['hit']*100:5.1f}%" if a["hit"] is not None else "  n/a"
    er = f"{a['exp_res']:+.2f}R" if a["exp_res"] is not None else "  n/a"
    ea = f"{a['exp_all']:+.2f}R" if a["exp_all"] is not None else "  n/a"
    return (f"n={a['n']:3d} (tp {a['tp']}/stop {a['stop']}/open {a['open']}) hit={h} "
            f"(BE {a['be']*100:.1f}%) expR(res)={er} expR(all)={ea} "
            f"MAE med/p75/max={a['mae_med']:.2f}/{a['mae_p75']:.2f}/{a['mae_max']:.2f} "
            f"stop_med={a['dist_med']:.2f}% t_med={a['t_med']}")


def basket_ret(d0, d1_):
    ds = sorted(k for k in btc_daily if d0 <= k < d1_)
    return (btc_daily[ds[-1]] / btc_daily[ds[0]] - 1) * 100 if len(ds) >= 2 else None


by = collections.defaultdict(list)
for ev in events:
    by[(ev["kind"], ev["split"])].append(ev)

TR_DAYS = (SPLIT - W0).days
VA_DAYS = (END - SPLIT).days
print(f"events total: {len(events)}   rejections: {dict(rejected)}")
print(f"regime BTC: train {basket_ret(W0, SPLIT):+.1f}%  validate {basket_ret(SPLIT, END):+.1f}%")
all_evs = events
print(f"effective-N (all types pooled): train={eff_n([e for e in all_evs if e['split']=='train'])} "
      f"validate={eff_n([e for e in all_evs if e['split']=='validate'])}")

for kind in ("A-reclaim", "B-higherlow", "C-failedbd"):
    tr, va = by[(kind, "train")], by[(kind, "validate")]
    print(f"\n===== {kind} =====")
    print(f"  fire rate: train {len(tr)}/{TR_DAYS}d = {len(tr)/TR_DAYS:.2f}/day · "
          f"validate {len(va)}/{VA_DAYS}d = {len(va)/VA_DAYS:.2f}/day · "
          f"effective-N tr={eff_n(tr)} va={eff_n(va)}")
    for tp_r in TP_RS:
        mark = "  <== CENTER (acceptance cell)" if tp_r == CENTER_R else ""
        print(f"  TP {tp_r}R{mark}")
        for split, evs in (("train", tr), ("validate", va)):
            print(f"    {split:8s} {fmt(agg(evs, tp_r))}")
    # acceptance check at center
    a_va = agg(va, CENTER_R)
    ok_mae = a_va["mae_med"] is not None and a_va["mae_med"] < 1.0
    ok_exp = a_va["exp_res"] is not None and a_va["exp_res"] > 0
    ok_rate = len(va) / VA_DAYS <= 1.0
    verdict = "PASS" if (ok_mae and ok_exp and ok_rate) else "FAIL"
    print(f"  ACCEPTANCE (validate, 3.2R): MAE<1R={ok_mae} expR>0={ok_exp} "
          f"rate<=1/day={ok_rate}  ==> {verdict}")

print("\nvalidate event lists (audit trail):")
for kind in ("A-reclaim", "B-higherlow", "C-failedbd"):
    for ev in sorted(by[(kind, "validate")], key=lambda e: e["date"]):
        r = resolve(ev, CENTER_R)
        print(f"  {ev['date']} {kind:12s} {ev['sym']:10s} entry {ev['entry']:g} "
              f"stop {ev['stop']:g} ({ev['dist']:.2f}%) -> {r['out']} "
              f"({r['r']:+.2f}R in {r['t']}d, MAE {r['mae']:.2f})")
