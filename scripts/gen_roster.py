#!/usr/bin/env python3
"""Full-universe roster generator (operator dispatch 2026-07-19).

Reads a raw Kraken AssetPairs snapshot (JSON) and rewrites the five roster
blocks in deepfield/config.py in place:

    PAIRS, SEED_PAIRS, PER_PAIR_LEVERAGE, MARGIN_PAIR, MARGIN_TICK_DECIMALS

Rules (dispatch is the authority):
  - every online USD-quoted pair with a non-empty leverage_buy joins, at
    max(leverage_buy) — no filtering, no second-guessing the roster
  - the 21 incumbent pairs keep their existing order and their PROBE-VERIFIED
    margin tick decimals (CRV=4, SHIB=8 are coarser than spot — hard-won)
  - new pairs start at spot pair_decimals; --exec-probe auto-coarsens to the
    :BTNL book's real precision and is the final authority
  - --exec-probe prunes: any pair Kraken rejects with Unknown-asset-pair on
    the validate probe is dropped by scripts/prune_roster.py, not by hand

Usage: gen_roster.py <assetpairs.json> [config_path] [--probe logs/exec_probe_journal.json]

With --probe, the probe journal is the FINAL authority: pairs it dropped are
removed from the roster and the tick decimals it verified replace the guesses.
"""
import json
import re
import sys

NORMALIZE = {"XBT": "BTC", "XDG": "DOGE"}

# Probe-verified :BTNL tick decimals for the incumbent roster (2026-07-13).
# These OVERRIDE spot pair_decimals — the margin book is coarser for CRV/SHIB.
VERIFIED_TICKS = {
    "BTC/USD": 1, "ETH/USD": 2, "XRP/USD": 5, "SOL/USD": 2, "DOGE/USD": 7,
    "ADA/USD": 6, "LINK/USD": 5, "SUI/USD": 4, "LTC/USD": 2, "AVAX/USD": 2,
    "AAVE/USD": 2, "UNI/USD": 3, "DOT/USD": 4, "BCH/USD": 2, "ALGO/USD": 5,
    "CRV/USD": 4, "HBAR/USD": 5, "PEPE/USD": 9, "SHIB/USD": 8,
    "TRX/USD": 6, "ZEC/USD": 2,
}

# Incumbent display order — the live 21 stay at the top of every table/UI.
INCUMBENT_ORDER = [
    "BTC/USD", "ETH/USD", "XRP/USD", "SOL/USD", "SUI/USD", "DOGE/USD",
    "LTC/USD", "LINK/USD", "ADA/USD", "AVAX/USD", "AAVE/USD", "UNI/USD",
    "DOT/USD", "BCH/USD", "ALGO/USD", "CRV/USD", "HBAR/USD", "PEPE/USD",
    "SHIB/USD", "TRX/USD", "ZEC/USD",
]


def norm_ws(wsname):
    base, quote = wsname.split("/")
    return f"{NORMALIZE.get(base, base)}/{quote}"


def build_roster(assetpairs):
    rows = []
    for rest, p in assetpairs.items():
        if p.get("quote") not in ("ZUSD", "USD"):
            continue
        if p.get("status") != "online":
            continue
        lb = p.get("leverage_buy") or []
        if not lb:
            continue
        ws = norm_ws(p["wsname"])
        rows.append({
            "rest": rest,
            "wsname": p["wsname"],
            "ws": ws,
            "display": ws.split("/")[0],
            "ordermin": float(p["ordermin"]),
            "costmin": float(p["costmin"]),
            "lev": max(lb),
            "alt": p["altname"],
            "tick": VERIFIED_TICKS.get(ws, int(p["pair_decimals"])),
        })
    order = {ws: i for i, ws in enumerate(INCUMBENT_ORDER)}
    rows.sort(key=lambda r: (order.get(r["ws"], len(order)), -r["lev"], r["ws"]))
    return rows


def fmt_num(x):
    """Numeric literal without float noise (1500000 not 1.5e+06 ... both valid,
    pick the plain form when exact)."""
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return repr(x)


def render_pairs(rows):
    out = ["PAIRS = ["]
    out.append("    # rest, wsname, ws, display, ordermin, costmin — full margin universe")
    out.append("    # (operator dispatch 2026-07-19: EVERY online USD margin pair, no filter).")
    out.append("    # ordermin/costmin are SEED/FALLBACK only — the pairs table refreshes from")
    out.append("    # AssetPairs at startup + daily. Incumbent 21 first, then by max-lev desc.")
    for r in rows:
        out.append(
            '    {"rest": %-14s "wsname": %-15s "ws": %-15s "display": %-11s '
            '"ordermin": %-9s "costmin": %s},' % (
                '"%s",' % r["rest"], '"%s",' % r["wsname"], '"%s",' % r["ws"],
                '"%s",' % r["display"], fmt_num(r["ordermin"]) + ",", fmt_num(r["costmin"])))
    out.append("]")
    return "\n".join(out)


def render_seed(rows):
    out = ["# ALL pairs are seed pairs (operator dispatch 2026-07-19: \"always-on ladders,"]
    out.append("# no signal gate\" across the entire margin universe — min-fill rungs; the")
    out.append("# margin cost of the lower-leverage tiers is operator-accepted). Regime gate,")
    out.append("# HALT, stack floor and all rung guards still apply per placement.")
    out.append("SEED_PAIRS = tuple(p[\"ws\"] for p in PAIRS)")
    return "\n".join(out)


def render_lev(rows):
    out = ["PER_PAIR_LEVERAGE = {"]
    for r in rows:
        out.append('    "%s": %d,' % (r["ws"], r["lev"]))
    out.append("}")
    return "\n".join(out)


def render_margin(rows):
    out = ["MARGIN_PAIR = {"]
    for r in rows:
        out.append('    "%s": "%s:BTNL",' % (r["ws"], r["alt"]))
    out.append("}")
    return "\n".join(out)


def render_ticks(rows):
    out = ["MARGIN_TICK_DECIMALS = {"]
    out.append("    # Incumbent 21: probe-verified 2026-07-13 (CRV/SHIB coarser than spot).")
    out.append("    # New pairs: spot pair_decimals as the starting guess — --exec-probe")
    out.append("    # auto-coarsens to the :BTNL book's accepted precision (the authority).")
    for r in rows:
        out.append('    "%s": %d,' % (r["ws"], r["tick"]))
    out.append("}")
    return "\n".join(out)


def splice(src, header_re, block, what):
    """Replace from the line matching header_re through its closing bracket line."""
    m = re.search(header_re, src, re.M)
    if not m:
        raise SystemExit(f"cannot find {what} block in config")
    start = m.start()
    # find the block's closing line: first line that is exactly ] or }
    tail = src[m.end():]
    cm = re.search(r"^[\]\}]\s*$", tail, re.M)
    if not cm:
        raise SystemExit(f"cannot find end of {what} block")
    end = m.end() + cm.end()
    return src[:start] + block + src[end:]


def splice_seed(src, block):
    """SEED_PAIRS: replace a literal `SEED_PAIRS = (...)` tuple (no bare closing
    line, so non-greedy through the first `)`). Once it's the derived
    `tuple(p["ws"] for p in PAIRS)` form, it tracks PAIRS automatically — leave
    it (and its comment block) untouched so re-runs are idempotent."""
    if 'SEED_PAIRS = tuple(p["ws"] for p in PAIRS)' in src:
        return src
    m = re.search(r"^SEED_PAIRS = \(.*?\)", src, re.M | re.S)
    if not m:
        raise SystemExit("cannot find SEED_PAIRS block in config")
    return src[:m.start()] + block + src[m.end():]


def main():
    args = list(sys.argv[1:])
    probe_path = None
    if "--probe" in args:
        i = args.index("--probe")
        probe_path = args[i + 1]
        del args[i:i + 2]
    ap_path = args[0]
    cfg_path = args[1] if len(args) > 1 else "deepfield/config.py"
    ap = json.load(open(ap_path))
    ap = ap.get("result", ap)
    rows = build_roster(ap)

    if probe_path:
        probe = json.load(open(probe_path))
        dropped = {d["pair"] for d in probe.get("dropped", [])}
        ticks = probe.get("ticks", {})
        before = len(rows)
        rows = [r for r in rows if r["ws"] not in dropped]
        for r in rows:
            if r["ws"] in ticks:
                r["tick"] = int(ticks[r["ws"]])
        print(f"probe merge: {before} -> {len(rows)} pairs ({len(dropped)} dropped), "
              f"{len(ticks)} probe-verified ticks applied")

    src = open(cfg_path).read()
    src = splice(src, r"^PAIRS = \[", render_pairs(rows), "PAIRS")
    src = splice_seed(src, render_seed(rows))
    src = splice(src, r"^PER_PAIR_LEVERAGE = \{", render_lev(rows), "PER_PAIR_LEVERAGE")
    src = splice(src, r"^MARGIN_PAIR = \{", render_margin(rows), "MARGIN_PAIR")
    src = splice(src, r"^MARGIN_TICK_DECIMALS = \{", render_ticks(rows), "MARGIN_TICK_DECIMALS")
    open(cfg_path, "w").write(src)

    # Roster journal
    print(f"{len(rows)} pairs")
    for r in rows:
        print(f"{r['ws']:13s} lev={r['lev']:2d} ordermin={r['ordermin']:<12g} "
              f"costmin={r['costmin']:g} tick={r['tick']} margin_pair={r['alt']}:BTNL")


if __name__ == "__main__":
    main()
