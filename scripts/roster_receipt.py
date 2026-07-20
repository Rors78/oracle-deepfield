#!/usr/bin/env python3
"""Receipt table for the full-universe roster (operator dispatch 2026-07-19).

pair | lev | rung size | rung cost $ | margin/rung $ — for every roster pair,
so the actual bank impact is visible per pair. Reads ONLY the local DB (pairs
table refreshed from AssetPairs by the latest backfill + newest closed candle);
no Kraken calls — never compete with the live bot for the account rate budget.

Rung sizing mirrors executor.size() min-mode exactly: ceil-to-lot-grid of
max(ordermin, costmin/price), scaled by SIZE_MULT rounded DOWN to the grid,
never below the min-fill floor (card=None seeds: conviction 1.0).
"""
import math
import sqlite3
import sys

sys.path.insert(0, ".")
from deepfield import config  # noqa: E402


def min_volume(ordermin, costmin, price, lot_dec):
    need = max(ordermin or 0.0, (costmin / price) if price > 0 else 0.0)
    if lot_dec is None:
        return need
    f = 10 ** lot_dec
    return math.ceil(need * f) / f


def rung_volume(ordermin, costmin, price, lot_dec):
    vol = min_volume(ordermin, costmin, price, lot_dec)
    smult = max(1.0, float(getattr(config, "SIZE_MULT", 1.0) or 1.0))
    if smult > 1.0 and vol > 0:
        scaled = vol * smult
        if lot_dec is not None:
            f = 10 ** lot_dec
            scaled = math.floor(scaled * f) / f
        vol = max(vol, scaled)
    return vol


def main():
    conn = sqlite3.connect("file:deepfield.db?mode=ro", uri=True)
    rows = []
    for p in config.PAIRS:
        ws = p["ws"]
        lev = config.PER_PAIR_LEVERAGE.get(ws)
        info = conn.execute(
            "SELECT ordermin, costmin, lot_decimals FROM pairs WHERE ws_symbol=?",
            (ws,)).fetchone()
        ordermin, costmin, lot_dec = info if info else (p["ordermin"], p["costmin"], None)
        px = conn.execute(
            "SELECT c FROM candles WHERE pair=? AND closed=1 ORDER BY ts DESC LIMIT 1",
            (ws,)).fetchone()
        px = px[0] if px else None
        if not px or not lev:
            rows.append((ws, lev, None, None, None))
            continue
        vol = rung_volume(ordermin, costmin, px, lot_dec)
        cost = vol * px
        rows.append((ws, lev, vol, cost, cost / lev))
    print(f"{'pair':13s} {'lev':>3s} {'rung size':>14s} {'rung cost $':>11s} {'margin/rung $':>13s}")
    tot_cost = tot_margin = 0.0
    for ws, lev, vol, cost, margin in rows:
        if vol is None:
            print(f"{ws:13s} {lev or '?':>3} {'NO DATA':>14s}")
            continue
        tot_cost += cost
        tot_margin += margin
        print(f"{ws:13s} {lev:>3d} {vol:>14.8g} {cost:>11.2f} {margin:>13.2f}")
    print("-" * 58)
    print(f"{'TOTAL (all rungs filled once)':30s} {tot_cost:>11.2f} {tot_margin:>13.2f}")
    print(f"\nSIZE_MULT={config.SIZE_MULT:g} · {len(rows)} pairs")


if __name__ == "__main__":
    main()
