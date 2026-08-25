#!/usr/bin/env python3
"""Compare PER_PAIR_LEVERAGE against Kraken's live AssetPairs leverage_buy max."""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from deepfield import config  # noqa: E402

rest_of = {p["ws"]: p["rest"] for p in config.PAIRS}
rests = ",".join(rest_of.values())
with urllib.request.urlopen(
        f"https://api.kraken.com/0/public/AssetPairs?pair={rests}", timeout=25) as r:
    res = json.loads(r.read())["result"]
by_rest = {}
for k, v in res.items():
    by_rest[k] = v
    by_rest[v.get("altname", k)] = v
print(f"{'pair':10s} {'config':>6s} {'kraken max':>10s}")
for ws, lev in config.PER_PAIR_LEVERAGE.items():
    d = by_rest.get(rest_of.get(ws))
    if not d:
        print(f"{ws:10s} {lev:6d} {'??':>10s}   (rest name {rest_of.get(ws)} not in response)")
        continue
    lb = d.get("leverage_buy") or []
    mx = max(lb) if lb else 1
    flag = "  <-- CHANGED" if mx != lev else ""
    print(f"{ws:10s} {lev:6d} {mx:10d}{flag}")
