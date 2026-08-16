"""Phase 1c: Study 1 (structural SHORT inversion) + Study 2 (wide-stop 1x longs).
Implements docs/PROP_INVERSION_WIDESTOPS_PROTOCOL_20260815.md (commit 66ab401)
EXACTLY. Cached 4h data + stored daily candles. No fetches, no writes.
"""
import sys, json, os, time, datetime, collections, statistics, sqlite3

sys.path.insert(0, "/home/golden/oracle-deepfield/.claude/worktrees/deck-sota-upscale")
from deepfield import config

CACHE = "/home/golden/.claude/jobs/a767fd5e/tmp/df_4h_cache.json"
BAR = 240 * 60
W_SPLIT = datetime.datetime(2026, 7, 21, tzinfo=datetime.timezone.utc)
W_END = datetime.datetime(2026, 8, 16, tzinfo=datetime.timezone.utc)
TP_RS = (2.5, 3.2, 4.0)
CENTER_R = 3.2
CAP_BARS = 84
RETEST_VALID = 12
FEE, FUND = 0.0004, 0.00033
NOW = time.time()

data = json.load(open(CACHE))


def wilder_atr(rows, i, hi=2, lo=3, cl=4):
    if i < 15:
        return None
    trs = []
    for j in range(1, i + 1):
        h, l, pc = rows[j][hi], rows[j][lo], rows[j - 1][cl]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:14]) / 14.0
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14.0
    return atr


def swing_lows(lows):
    return [(i, lows[i], i + 1) for i in range(1, len(lows) - 1)
            if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]]


def triggers_for(bars):
    """Yield (kind, T, level, m1_stop, atr) — identical detection to Phase 1b."""
    lows = [b[3] for b in bars]
    closes = [b[4] for b in bars]
    swings = swing_lows(lows)
    for T in range(25, len(bars)):
        known = [s for s in swings if s[2] <= T]
        atr = wilder_atr(bars, T)
        if atr is None:
            continue
        if known:
            for pv, L, _ka in reversed(known):
                if pv >= T:
                    continue
                if any(closes[j] < L for j in range(max(0, T - 10), T)):
                    if closes[T] > L and closes[T - 1] <= L:
                        yield ("A", T, L, L - 0.5 * atr, atr)
                    break
        just = [s for s in known if s[2] == T]
        if just:
            pv2, S2, _ = just[-1]
            older = [s for s in known if s[2] < T and s[0] < pv2]
            if older and S2 > older[-1][1]:
                yield ("B", T, S2, S2, atr)
        if T >= 22:
            L20 = min(lows[T - 21: T - 1])
            if closes[T - 1] < L20 and closes[T] > L20:
                yield ("C", T, L20, min(lows[T - 1], lows[T]), atr)


def resolve_long(entry, stop, path, tp_r, pre_mae=0.0, fillbar_stopped=False):
    tp = entry + tp_r * (entry - stop)
    if fillbar_stopped:
        return {"out": "stop", "r": -1.0, "t": 0, "mae": pre_mae}
    mae = pre_mae
    for n, (o, h, l, c) in enumerate(path, 1):
        mae = max(mae, (entry - l) / (entry - stop))
        if l <= stop:
            return {"out": "stop", "r": -1.0, "t": n, "mae": mae}
        if h >= tp:
            return {"out": "tp", "r": tp_r, "t": n, "mae": mae}
    last = path[-1][3] if path else entry
    return {"out": "open", "r": (last - entry) / (entry - stop), "t": len(path), "mae": mae}


def resolve_short(entry, stop, path, tp_r, pre_mae=0.0, fillbar_stopped=False):
    tp = entry - tp_r * (stop - entry)
    if fillbar_stopped:
        return {"out": "stop", "r": -1.0, "t": 0, "mae": pre_mae}
    mae = pre_mae
    for n, (o, h, l, c) in enumerate(path, 1):
        mae = max(mae, (h - entry) / (stop - entry))
        if h >= stop:
            return {"out": "stop", "r": -1.0, "t": n, "mae": mae}
        if l <= tp:
            return {"out": "tp", "r": tp_r, "t": n, "mae": mae}
    last = path[-1][3] if path else entry
    return {"out": "open", "r": (entry - last) / (stop - entry), "t": len(path), "mae": mae}


def gate_long(entry, stop, tp_r):
    tp = entry + tp_r * (entry - stop)
    loss = (entry - stop) + FEE * entry + FEE * stop + FUND * entry
    gain = (tp - entry) - FEE * entry - FEE * tp - FUND * entry
    return gain / loss >= 3.0 if loss > 0 else False


def gate_short(entry, stop, tp_r):
    tp = entry - tp_r * (stop - entry)
    loss = (stop - entry) + FEE * entry + FEE * stop + FUND * entry
    gain = (entry - tp) - FEE * entry - FEE * tp - FUND * entry
    return gain / loss >= 3.0 if loss > 0 else False


def agg(rows, tp_r, short=False):
    """rows: (entry, stop, path, pre_mae, fillbar_stopped, dist_pct)."""
    fn = resolve_short if short else resolve_long
    gt = gate_short if short else gate_long
    rs = [fn(e, s, p, tp_r, pm, fs) for (e, s, p, pm, fs, _d) in rows]
    n = len(rs)
    tps = [r for r in rs if r["out"] == "tp"]
    stops = [r for r in rs if r["out"] == "stop"]
    opens = [r for r in rs if r["out"] == "open"]
    resolved = len(tps) + len(stops)
    maes = sorted(r["mae"] for r in rs)
    ts = sorted(r["t"] for r in tps + stops)
    dists = sorted(d for (*_x, d) in rows)
    return {"n": n, "tp": len(tps), "stop": len(stops), "open": len(opens),
            "hit": len(tps) / resolved if resolved else None, "be": 1 / (1 + tp_r),
            "exp_res": (len(tps) * tp_r - len(stops)) / resolved if resolved else None,
            "exp_all": sum(r["r"] for r in rs) / n if n else None,
            "mae_med": statistics.median(maes) if maes else None,
            "mae_p75": maes[int(0.75 * (len(maes) - 1))] if maes else None,
            "mae_max": maes[-1] if maes else None,
            "dist_med": statistics.median(dists) if dists else None,
            "dist_p75": dists[int(0.75 * (len(dists) - 1))] if dists else None,
            "dist_max": dists[-1] if dists else None,
            "t_med": statistics.median(ts) if ts else None,
            "gate": sum(gt(e, s, tp_r) for (e, s, *_r) in rows)}


def eff_n(dates):
    if not dates:
        return 0
    spans = sorted((d, d + datetime.timedelta(days=14)) for d in dates)
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
    return (f"n={a['n']:4d} (tp {a['tp']}/st {a['stop']}/op {a['open']}) hit={h} "
            f"(BE {a['be']*100:.1f}%) expR(res)={er} expR(all)={ea} "
            f"MAE={a['mae_med']:.2f}/{a['mae_p75']:.2f}/{a['mae_max']:.2f} "
            f"stop%={a['dist_med']:.2f}/{a['dist_p75']:.2f}/{a['dist_max']:.2f} "
            f"gate={a['gate']}/{a['n']} t_med={a['t_med']}")


# ── collect events off cached 4h ─────────────────────────────────────────────
S1 = collections.defaultdict(list)      # (cell, split) -> rows ; cells A-s/B-s/C-s/D
S1_dates = collections.defaultdict(list)
S1_rej = collections.Counter()
S1_unf = collections.Counter()
S1_D_src = collections.Counter()
S2_4h = collections.defaultdict(list)   # (kind, mode, split) -> rows (uncapped longs)
S2_dates = collections.defaultdict(list)
S2_deg = collections.Counter()

for sym, bars0 in data.items():
    bars = [b for b in bars0 if b[0] + BAR <= NOW]
    if len(bars) < 60:
        continue
    lows = [b[3] for b in bars]
    for kind, T, level, m1_stop, atr in triggers_for(bars):
        ts = bars[T][0] + BAR
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        if dt >= W_END:
            continue
        split = "train" if dt < W_SPLIT else "validate"
        date = dt.date()
        # shared retest fill scan
        fill_k = None
        for k in range(T + 1, min(T + 1 + RETEST_VALID, len(bars))):
            if lows[k] <= level:
                fill_k = k
                break
        # ── STUDY 1: retest-short ──
        s_stop = level + 0.5 * atr
        dist_s = (s_stop - level) / level * 100
        if s_stop <= level:
            S1_rej[(kind, "degenerate")] += 1
        elif dist_s > 8.4:
            S1_rej[(kind, "stop>8.4%")] += 1
        elif fill_k is None:
            S1_unf[(kind, split)] += 1
        else:
            fb = bars[fill_k]
            fillbar_stopped = fb[2] >= s_stop
            pre_mae = (fb[2] - level) / (s_stop - level)
            path = [(bars[k][1], bars[k][2], bars[k][3], bars[k][4])
                    for k in range(fill_k + 1, min(fill_k + 1 + CAP_BARS, len(bars)))]
            S1[(kind + "-s", split)].append((level, s_stop, path, pre_mae, fillbar_stopped, dist_s))
            S1_dates[(kind + "-s", split)].append(date)
            # ── event D: breach-close short ──
            if fb[4] < level:
                e_d = fb[4]
                if s_stop > e_d and (s_stop - e_d) / e_d * 100 <= 8.4:
                    path_d = [(bars[k][1], bars[k][2], bars[k][3], bars[k][4])
                              for k in range(fill_k + 1, min(fill_k + 1 + CAP_BARS, len(bars)))]
                    S1[("D", split)].append((e_d, s_stop, path_d, 0.0, False,
                                             (s_stop - e_d) / e_d * 100))
                    S1_dates[("D", split)].append(date)
                    S1_D_src[kind] += 1
        # ── STUDY 2: uncapped longs, both modes ──
        e1 = bars[T][4]
        if m1_stop < e1:
            d1 = (e1 - m1_stop) / e1 * 100
            path1 = [(bars[k][1], bars[k][2], bars[k][3], bars[k][4])
                     for k in range(T + 1, min(T + 1 + CAP_BARS, len(bars)))]
            S2_4h[(kind, "m1", split)].append((e1, m1_stop, path1, 0.0, False, d1))
            S2_dates[(kind, "m1", split)].append(date)
        else:
            S2_deg[(kind, "m1")] += 1
        l_stop = level - 0.5 * atr
        if l_stop < level and fill_k is not None:
            fb = bars[fill_k]
            d2 = (level - l_stop) / level * 100
            fillbar_stopped = fb[3] <= l_stop
            pre_mae = (level - fb[3]) / (level - l_stop)
            path2 = [(bars[k][1], bars[k][2], bars[k][3], bars[k][4])
                     for k in range(fill_k + 1, min(fill_k + 1 + CAP_BARS, len(bars)))]
            S2_4h[(kind, "m2", split)].append((level, l_stop, path2, pre_mae, fillbar_stopped, d2))
            S2_dates[(kind, "m2", split)].append(date)

# ── STUDY 2 daily rerun (mode 1, uncapped) off the live DB ──────────────────
conn = sqlite3.connect("file:/home/golden/oracle-deepfield/deepfield.db?mode=ro", uri=True)
conn.execute("PRAGMA query_only=1")
S2_D = collections.defaultdict(list)
S2_D_dates = collections.defaultdict(list)
W0d = datetime.date(2026, 5, 17)
for p in config.PAIRS:
    sym = p["ws"]
    d = conn.execute("SELECT ts,o,h,l,c FROM candles WHERE pair=? AND interval=1440 "
                     "AND closed=1 ORDER BY ts", (sym,)).fetchall()
    if len(d) < 60:
        continue
    for kind, T, level, m1_stop, atr in triggers_for(d):
        date = datetime.datetime.fromtimestamp(d[T][0] + 86400, datetime.timezone.utc).date()
        if date < W0d or date >= W_END.date():
            continue
        split = "train" if date < W_SPLIT.date() else "validate"
        e1 = d[T][4]
        if m1_stop >= e1:
            continue
        dist = (e1 - m1_stop) / e1 * 100
        path = [(d[k][1], d[k][2], d[k][3], d[k][4])
                for k in range(T + 1, min(T + 15, len(d)))]
        S2_D[(kind, split)].append((e1, m1_stop, path, 0.0, False, dist))
        S2_D_dates[(kind, split)].append(date)
conn.close()

TR_DAYS = (W_SPLIT.date() - datetime.date(2026, 4, 18)).days
VA_DAYS = (W_END.date() - W_SPLIT.date()).days
TR_DAYS_D = (W_SPLIT.date() - W0d).days

print("═══ STUDY 1 — structural SHORT inversion (4h, retest fills) ═══")
print(f"rejections: {dict(S1_rej)} · unfilled: {dict(S1_unf)} · D sources: {dict(S1_D_src)}")
for cell in ("A-s", "B-s", "C-s", "D"):
    print(f"\n----- {cell} -----")
    tr, va = S1[(cell, "train")], S1[(cell, "validate")]
    print(f"  fills/day: tr {len(tr)/TR_DAYS:.2f} va {len(va)/VA_DAYS:.2f} · "
          f"effN tr={eff_n(S1_dates[(cell,'train')])} va={eff_n(S1_dates[(cell,'validate')])}")
    for tp_r in TP_RS:
        mark = "  <== CENTER" if tp_r == CENTER_R else ""
        print(f"  TP {tp_r}R{mark}")
        for split, rows in (("train", tr), ("validate", va)):
            print(f"    {split:8s} {fmt(agg(rows, tp_r, short=True))}")
    a_va = agg(va, CENTER_R, short=True)
    ok = (a_va["mae_med"] is not None and a_va["mae_med"] < 1.0,
          a_va["exp_res"] is not None and a_va["exp_res"] > 0,
          len(va) / VA_DAYS <= 1.0)
    print(f"  ACCEPTANCE (va 3.2R): MAE<1R={ok[0]} expR>0={ok[1]} rate<=1/day={ok[2]} "
          f"==> {'PASS' if all(ok) else 'FAIL'}")

print("\n═══ STUDY 2 — wide-stop 1x longs (8.4% cap REMOVED) ═══")
for kind in ("A", "B", "C"):
    for mode in ("m1", "m2"):
        print(f"\n----- {kind}-{mode} (4h) -----")
        tr, va = S2_4h[(kind, mode, "train")], S2_4h[(kind, mode, "validate")]
        print(f"  fires/day: tr {len(tr)/TR_DAYS:.2f} va {len(va)/VA_DAYS:.2f} · "
              f"effN tr={eff_n(S2_dates[(kind,mode,'train')])} va={eff_n(S2_dates[(kind,mode,'validate')])}")
        for split, rows in (("train", tr), ("validate", va)):
            a = agg(rows, CENTER_R)
            med_not = 40.0 / (a["dist_med"] / 100) if a["dist_med"] else 0
            print(f"    {split:8s} 3.2R {fmt(a)}  notional@$40risk med=${med_not:,.0f}")
        a_va = agg(va, CENTER_R)
        ok = (a_va["mae_med"] is not None and a_va["mae_med"] < 1.0,
              a_va["exp_res"] is not None and a_va["exp_res"] > 0,
              len(va) / VA_DAYS <= 1.0)
        print(f"  ACCEPTANCE (va 3.2R): MAE<1R={ok[0]} expR>0={ok[1]} rate<=1/day={ok[2]} "
              f"==> {'PASS' if all(ok) else 'FAIL'}")

print("\n----- daily rerun, mode 1, uncapped -----")
for kind in ("A", "B", "C"):
    tr, va = S2_D[(kind, "train")], S2_D[(kind, "validate")]
    print(f"  {kind}: fires/day tr {len(tr)/TR_DAYS_D:.2f} va {len(va)/VA_DAYS:.2f} · "
          f"effN va={eff_n(S2_D_dates[(kind,'validate')])}")
    for split, rows in (("train", tr), ("validate", va)):
        print(f"    {split:8s} 3.2R {fmt(agg(rows, CENTER_R))}")
    a_va = agg(va, CENTER_R)
    ok = (a_va["mae_med"] is not None and a_va["mae_med"] < 1.0,
          a_va["exp_res"] is not None and a_va["exp_res"] > 0,
          len(va) / VA_DAYS <= 1.0)
    print(f"  ACCEPTANCE: {'PASS' if all(ok) else 'FAIL'} (MAE<1R={ok[0]} expR>0={ok[1]} rate={ok[2]})")
