"""4h close-entry vs retest-entry structural study (Phase 1b).
Implements docs/PROP_STRUCTURAL_4H_PROTOCOL_20260815.md (commit dcbaf28) EXACTLY.
Public Kraken 4h OHLC, fetched once, cached. No account API, no writes.
"""
import sys, json, time, os, datetime, collections, statistics, urllib.request

sys.path.insert(0, "/home/golden/oracle-deepfield/.claude/worktrees/deck-sota-upscale")
from deepfield import config

CACHE = "/home/golden/.claude/jobs/a767fd5e/tmp/df_4h_cache.json"
BAR = 240 * 60
W_SPLIT = datetime.datetime(2026, 7, 21, tzinfo=datetime.timezone.utc)
W_END = datetime.datetime(2026, 8, 16, tzinfo=datetime.timezone.utc)
TP_RS = (2.5, 3.2, 4.0)
CENTER_R = 3.2
CAP_BARS = 84            # 14 days of 4h bars
RETEST_VALID = 12
MAX_STOP_PCT = 8.4

if os.path.exists(CACHE):
    data = json.load(open(CACHE))
    print(f"cache hit: {len(data)} pairs")
else:
    data = {}
    for p in config.PAIRS:
        rest = p["rest"]
        url = f"https://api.kraken.com/0/public/OHLC?pair={rest}&interval=240"
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                res = json.load(r).get("result", {})
            arr = next((v for k, v in res.items() if k != "last"), [])
            bars = [(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]))
                    for c in arr]
            if len(bars) >= 60:
                data[p["ws"]] = bars
        except Exception as e:
            print(f"fetch failed {rest}: {e}")
        time.sleep(0.35)
    json.dump(data, open(CACHE, "w"))
    print(f"fetched {len(data)} pairs -> cache")

NOW = time.time()


def wilder_atr(rows, i):
    if i < 15:
        return None
    trs = []
    for j in range(1, i + 1):
        h, l, pc = rows[j][2], rows[j][3], rows[j][4 - 0] if False else rows[j - 1][4]
        h, l = rows[j][2], rows[j][3]
        pc = rows[j - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:14]) / 14.0
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14.0
    return atr


events, rejected = [], collections.Counter()
unfilled = collections.Counter()
btc_4h = {}
coverage = {}

for sym, bars0 in data.items():
    bars = [b for b in bars0 if b[0] + BAR <= NOW]        # drop forming bar
    if len(bars) < 60:
        continue
    lows = [b[3] for b in bars]
    closes = [b[4] for b in bars]
    coverage[sym] = (datetime.datetime.fromtimestamp(bars[0][0], datetime.timezone.utc).date(),
                     datetime.datetime.fromtimestamp(bars[-1][0] + BAR, datetime.timezone.utc).date())
    if sym == "BTC/USD":
        for b in bars:
            btc_4h[b[0] + BAR] = b[4]
    swings = []
    for i in range(1, len(bars) - 1):
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            swings.append((i, lows[i], i + 1))

    def trig(kind, T, level, m1_stop):
        ts = bars[T][0] + BAR
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        if dt >= W_END:
            return
        split = "train" if dt < W_SPLIT else "validate"
        atr = wilder_atr(bars, T)
        if atr is None:
            return
        # mode 1
        e1 = closes[T]
        if m1_stop >= e1:
            rejected[(kind, "m1", "degenerate")] += 1
        elif (e1 - m1_stop) / e1 * 100 > MAX_STOP_PCT:
            rejected[(kind, "m1", "stop>8.4%")] += 1
        else:
            path = [(bars[k][1], bars[k][2], bars[k][3], bars[k][4])
                    for k in range(T + 1, min(T + 1 + CAP_BARS, len(bars)))]
            events.append({"kind": kind, "mode": "m1", "sym": sym, "dt": dt, "split": split,
                           "entry": e1, "stop": m1_stop, "level": level,
                           "path": path, "fillbar_stop": False})
        # mode 2 — limit at level, valid T+1..T+12
        s2 = level - 0.5 * atr
        if s2 >= level:
            rejected[(kind, "m2", "degenerate")] += 1
            return
        if (level - s2) / level * 100 > MAX_STOP_PCT:
            rejected[(kind, "m2", "stop>8.4%")] += 1
            return
        fill_k = None
        for k in range(T + 1, min(T + 1 + RETEST_VALID, len(bars))):
            if lows[k] <= level:
                fill_k = k
                break
        if fill_k is None:
            unfilled[(kind, split)] += 1
            return
        fb = bars[fill_k]
        fillbar_stopped = fb[3] <= s2
        path = [(bars[k][1], bars[k][2], bars[k][3], bars[k][4])
                for k in range(fill_k + 1, min(fill_k + 1 + CAP_BARS, len(bars)))]
        events.append({"kind": kind, "mode": "m2", "sym": sym, "dt": dt, "split": split,
                       "entry": level, "stop": s2, "level": level,
                       "path": path, "fillbar_stop": fillbar_stopped,
                       "fillbar_low": fb[3]})

    for T in range(25, len(bars)):
        known = [s for s in swings if s[2] <= T]
        # A
        if known:
            for pv, L, _ka in reversed(known):
                if pv >= T:
                    continue
                if any(closes[j] < L for j in range(max(0, T - 10), T)):
                    if closes[T] > L and closes[T - 1] <= L:
                        atr = wilder_atr(bars, T)
                        if atr:
                            trig("A-reclaim", T, L, L - 0.5 * atr)
                    break
        # B
        just = [s for s in known if s[2] == T]
        if just:
            pv2, S2, _ = just[-1]
            older = [s for s in known if s[2] < T and s[0] < pv2]
            if older and S2 > older[-1][1]:
                trig("B-higherlow", T, S2, S2)
        # C
        if T >= 22:
            L20 = min(lows[T - 21: T - 1])
            if closes[T - 1] < L20 and closes[T] > L20:
                trig("C-failedbd", T, L20, min(lows[T - 1], lows[T]))


def resolve(ev, tp_r):
    e, s = ev["entry"], ev["stop"]
    tp = e + tp_r * (e - s)
    if ev["fillbar_stop"]:
        mae0 = (e - ev["fillbar_low"]) / (e - s)
        return {"out": "stop", "r": -1.0, "t": 0, "mae": mae0}
    mae = (e - ev.get("fillbar_low", e)) / (e - s) if ev["mode"] == "m2" else 0.0
    for n, (o, h, l, c) in enumerate(ev["path"], 1):
        mae = max(mae, (e - l) / (e - s))
        if l <= s:
            return {"out": "stop", "r": -1.0, "t": n, "mae": mae}
        if h >= tp:
            return {"out": "tp", "r": tp_r, "t": n, "mae": mae}
    last_c = ev["path"][-1][3] if ev["path"] else e
    return {"out": "open", "r": (last_c - e) / (e - s), "t": len(ev["path"]), "mae": mae}


def gap_r(ev):
    return (ev["entry"] - ev["level"]) / (ev["entry"] - ev["stop"]) if ev["entry"] != ev["stop"] else 0.0


def agg(evs, tp_r):
    rs = [resolve(ev, tp_r) for ev in evs]
    n = len(rs)
    tps = [r for r in rs if r["out"] == "tp"]
    stops = [r for r in rs if r["out"] == "stop"]
    opens = [r for r in rs if r["out"] == "open"]
    resolved = len(tps) + len(stops)
    maes = sorted(r["mae"] for r in rs)
    ts = sorted(r["t"] for r in tps + stops)
    dists = sorted((ev["entry"] - ev["stop"]) / ev["entry"] * 100 for ev in evs)
    gaps = sorted(gap_r(ev) for ev in evs)
    return {"n": n, "tp": len(tps), "stop": len(stops), "open": len(opens),
            "hit": len(tps) / resolved if resolved else None, "be": 1 / (1 + tp_r),
            "exp_res": (len(tps) * tp_r - len(stops)) / resolved if resolved else None,
            "exp_all": sum(r["r"] for r in rs) / n if n else None,
            "mae_med": statistics.median(maes) if maes else None,
            "mae_p75": maes[int(0.75 * (len(maes) - 1))] if maes else None,
            "mae_max": maes[-1] if maes else None,
            "dist_med": statistics.median(dists) if dists else None,
            "gap_med": statistics.median(gaps) if gaps else None,
            "gap_p75": gaps[int(0.75 * (len(gaps) - 1))] if gaps else None,
            "t_med": statistics.median(ts) if ts else None}


def eff_n(evs):
    if not evs:
        return 0
    spans = sorted((ev["dt"].date(), ev["dt"].date() + datetime.timedelta(days=14)) for ev in evs)
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
            f"MAE={a['mae_med']:.2f}/{a['mae_p75']:.2f}/{a['mae_max']:.2f} "
            f"stop_med={a['dist_med']:.2f}% gapR={a['gap_med']:.2f}/{a['gap_p75']:.2f} "
            f"t_med={a['t_med']}")


by = collections.defaultdict(list)
for ev in events:
    by[(ev["kind"], ev["mode"], ev["split"])].append(ev)

d0 = min(c[0] for c in coverage.values())
print(f"coverage: {d0} -> 2026-08-15 · pairs {len(coverage)} · events {len(events)}")
print(f"rejections: {dict(rejected)}")
print(f"unfilled mode-2 triggers: {dict(unfilled)}")
ts_sorted = sorted(btc_4h)
tr_ts = [t for t in ts_sorted if datetime.datetime.fromtimestamp(t, datetime.timezone.utc) < W_SPLIT]
va_ts = [t for t in ts_sorted if datetime.datetime.fromtimestamp(t, datetime.timezone.utc) >= W_SPLIT]
if tr_ts and va_ts:
    print(f"regime BTC: train {(btc_4h[tr_ts[-1]]/btc_4h[tr_ts[0]]-1)*100:+.1f}%  "
          f"validate {(btc_4h[va_ts[-1]]/btc_4h[va_ts[0]]-1)*100:+.1f}%")

TR_DAYS = (W_SPLIT.date() - d0).days
VA_DAYS = (W_END.date() - W_SPLIT.date()).days

for kind in ("C-failedbd", "A-reclaim", "B-higherlow"):     # lead with C
    print(f"\n===== {kind} =====")
    for mode in ("m1", "m2"):
        tr, va = by[(kind, mode, "train")], by[(kind, mode, "validate")]
        trig_tr = len(tr) + unfilled.get((kind, "train"), 0) if mode == "m2" else len(tr)
        trig_va = len(va) + unfilled.get((kind, "validate"), 0) if mode == "m2" else len(va)
        fill_note = ""
        if mode == "m2":
            fill_note = (f" · fill rate tr {len(tr)}/{trig_tr}={len(tr)/trig_tr*100 if trig_tr else 0:.0f}% "
                         f"va {len(va)}/{trig_va}={len(va)/trig_va*100 if trig_va else 0:.0f}%")
        print(f"  [{mode}] fires/day: tr {len(tr)/TR_DAYS:.2f} va {len(va)/VA_DAYS:.2f} · "
              f"effN tr={eff_n(tr)} va={eff_n(va)}{fill_note}")
        for tp_r in TP_RS:
            mark = "  <== CENTER" if tp_r == CENTER_R else ""
            print(f"    TP {tp_r}R{mark}")
            for split, evs in (("train", tr), ("validate", va)):
                print(f"      {split:8s} {fmt(agg(evs, tp_r))}")
        a_va = agg(va, CENTER_R)
        ok = (a_va["mae_med"] is not None and a_va["mae_med"] < 1.0,
              a_va["exp_res"] is not None and a_va["exp_res"] > 0,
              len(va) / VA_DAYS <= 1.0)
        print(f"    ACCEPTANCE (va 3.2R): MAE<1R={ok[0]} expR>0={ok[1]} rate<=1/day={ok[2]} "
              f"==> {'PASS' if all(ok) else 'FAIL'}")

print("\nC-failedbd mode-2 validate audit trail (center R):")
for ev in sorted(by[("C-failedbd", "m2", "validate")], key=lambda e: e["dt"]):
    r = resolve(ev, CENTER_R)
    print(f"  {ev['dt']:%m-%d %H:%M} {ev['sym']:10s} level {ev['level']:g} stop {ev['stop']:g} "
          f"-> {r['out']} ({r['r']:+.2f}R in {r['t']} bars, MAE {r['mae']:.2f})")
