# DEEPFIELD AUDIT — EVIDENCE PACK (READ-ONLY)

**Pull timestamp:** 2026-07-16 12:39:52 MDT (18:39:52 UTC)
**Method:** live Kraken private API (GET-style endpoints only — TradeBalance, OpenPositions,
OpenOrders, ClosedOrders, Ledgers, TradesHistory) via `deepfield.broker.private()`, plus local
`deepfield.db` (journal / equity_history) and codebase grep. **No orders placed, canceled, or
modified. No positions touched. No code changed.**

Live bot confirmed running throughout: PID 3320, `DEEPFIELD_EXEC_MODE=live`.

Numbers below are the raw API/ledger truth at pull time. Values marked **DERIVED** are computed
here (arithmetic shown); values marked **UNVERIFIED** could not be sourced from the live API.

> Note on "10 positions": the exchange app's "10" is **10 pairs**. Kraken's OpenPositions returns
> **30 open lots** (one row per fill, unconsolidated). All tables below consolidate to the 10 pairs
> and note the lot count.

---

## SECTION A — ACCOUNT STATE

Raw `TradeBalance` (asset ZUSD), pulled 12:39:52 MDT:

```json
{ "eb": "212.0691",   // equivalent balance (cash + deposits)
  "tb": "212.0691",   // trade balance
  "m":  "151.1935",   // margin currently used
  "n":  "-11.3794",   // unrealized net P&L (open positions)
  "c":  "1511.9338",  // cost basis of open positions
  "v":  "1500.5541",  // current value of open positions
  "e":  "200.6894",   // EQUITY (eb + n)
  "mf": "49.4959",    // free margin
  "mfo":"-0.8571",
  "ml": "132.73" }    // MARGIN LEVEL %
```

| field | value |
|---|---|
| Equity (`e`) | **200.6894 USDT** |
| Used margin (`m`) | 151.1935 USDT |
| Free margin (`mf`) | 49.4959 USDT |
| Margin level (`ml`) | **132.73 %** |
| Unrealized P&L (`n`) | −11.3794 USDT |
| Open-position notional (`v`) | 1500.5541 USDT |
| Margin utilization (`m`/`e`) — **DERIVED** | **75.3 %** |
| Effective book leverage (`v`/`e`) — **DERIVED** | **7.48×** |

**+40 deposit — CONFIRMED in ledger.** Exactly one `deposit` entry in the 24h window:

```json
{ "type": "deposit", "asset": "ZUSD", "amount": "40.0000", "fee": "1.7500",
  "balance": "212.0691", "time": 1784224912.95, "refid": "FTJBoux-dDb1oNLmL42nNqBxhiPzh5" }
```

- Booked **2026-07-16 12:01:52 MDT**, balance after = 212.0691. ⚠️ The dispatch put the deposit at
  ~12:19 local; the ledger books it at **12:01:52**. Minor timing discrepancy — flagging, not resolving.
- The deposit **cost a 1.75 USDT fee** (a wire/instant-funding fee). Net cash added = **+38.25**.

**Does DEEPFIELD ingest the new equity?**
- **YES, within one poll cycle.** The app loop calls `trade_balance_full()` live every cycle and
  persists `equity / margin_used / free_margin / margin_level` to state + the web-live blob
  (`app.py:207–362`). equity_history shows 206.91 @ 12:24:52 (post-deposit) — the new balance is live.
- **Position sizing does NOT read equity for order size.** In the active `EXEC_SIZE_MODE="min"` path,
  order volume = `SIZE_MULT × min_fill` per pair — a fixed size independent of balance
  (`executor.py:245–263`). Live equity is read only for the (disabled) kill-switch rail and the
  `MARGIN_CAP_PCT` per-order cap. **Consequence: the +40 deposit raised the margin cushion (ml, free
  margin) but changed no order sizes.** More equity → more headroom to keep stacking the same-sized rungs.

---

## SECTION B — PER-POSITION TABLE

Consolidated to 10 pairs (30 lots). **All positions are side = BUY (long), leverage = 10:1.**
`mark~` = value/vol; `entry~` = cost/vol. Sorted by `mark→stop %` ascending (thinnest cushion first).

| pair | lots | size (vol) | entry~ | mark~ | unreal $ | margin $ | notional $ | #stops | stop px | cover % | mark→stop % |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| ETH  | 4 | 0.064 | 1892.9 | 1872 | −1.341 | 12.11 | 119.81 | 4 | 1731.11 | 100% | **+7.5%** |
| SOL  | 3 | 2.88 | 76.66 | 75.47 | −3.427 | 22.08 | 217.35 | 3 | 69.76 | 100% | **+7.6%** |
| XRP  | 4 | 79.2 | 1.1039 | 1.0882 | −1.246 | 8.74 | 86.18 | 3 | 1.00452 | 100% | +7.7% |
| LINK | 3 | 26.4 | 8.4578 | 8.376 | −2.161 | 22.33 | 221.13 | 3 | 7.69639 | 100% | +8.1% |
| AVAX | 4 | 24 | 6.62 | 6.555 | −1.560 | 15.89 | 157.32 | 3 | 6.02 | 100% | +8.2% |
| ADA  | 3 | 960 | 0.16313 | 0.16191 | −1.171 | 15.66 | 155.43 | 3 | 0.14844 | 100% | +8.3% |
| SUI  | 3 | 240 | 0.74813 | 0.74611 | −0.486 | 17.96 | 179.07 | 3 | 0.6808 | 100% | +8.8% |
| XBT  | 2 | 0.0016 | 64472 | 64105 | −0.587 | 10.32 | 102.57 | 2 | 58374.5 | 100% | +8.9% |
| LTC  | 2 | 3.2 | 44.795 | 45.16 | +1.169 | 14.33 | 144.51 | 2 | 41.14 | 100% | +8.9% |
| XDG  | 2 | 1600 | 0.0736 | 0.073244 | −0.570 | 11.78 | 117.19 | 2 | 0.0666394 | 100% | +9.0% |
| **TOTAL** | **30** | | | | **−11.379** | **151.19** | **1500.55** | **28** | | | |

**Nothing under 20% mark→stop** — the stops sit ~7.5–9.0% below mark, a normal band. **But the stop
distance is NOT the liquidation distance** (see below). Nothing is naked; all 10 pairs are 100% stop-covered.

**Per-position liquidation price — UNVERIFIED / NOT AVAILABLE.** Kraken's OpenPositions payload
carries `cost / fee / vol / margin / value / net / terms / rollovertm` — **no `liqprice` field**
(sample row below). Liquidation on Kraken is account-level (margin-level driven), not per-lot, so a
per-position liq price cannot be pulled from the API.

```json
// sample OpenPositions lot (XRP)
{ "pair":"XRPUSD:BTNL","type":"buy","ordertype":"limit","cost":"20.45519094",
  "fee":"0.07159317","vol":"18.71780434","vol_closed":"0.00000000","margin":"2.04551909",
  "value":"20.36796597","net":"-0.0872","terms":"0.0500% per 4 hours","rollovertm":"1784238917" }
```

**Account-level liquidation distance — DERIVED** (ml = equity / used_margin; used_margin 151.19 fixed):

| threshold | ml | equity at threshold | equity drop | **basket move to trigger** |
|---|--:|--:|--:|--:|
| Margin call | 80% | 120.95 | −39.7% | **−5.31 %** |
| Force-liquidation | 40% | 60.48 | −69.9% | **−9.34 %** |

> **The buffer, stated plainly:** a **−5.31%** move in the basket margin-calls the account; **−9.34%**
> force-liquidates it. The protective stops sit **~7.5–9.0%** below mark. So Kraken **margin-calls
> ~5.3% down — ABOVE where any stop fires (~8%)** — and force-liquidation (~9.3%) sits *just past* the
> stop band. In a fast gap-down, liquidation and the market-stop fills are effectively on top of each
> other; the stops do not clear the liquidation line with meaningful room.

---

## SECTION C — ORDER / POSITION JOIN

**38 open orders, fully classified. Zero orphans. Zero naked positions. Zero unknowns.**

| class | count | note |
|---|--:|---|
| ladder-buy (post-only limit, by design) | **10** | one resting rung per pair, `oflags=post,fciq` |
| stop-loss sell, attached to a live position | **28** | `ordertype=stop-loss`, `oflags=fciq` |
| ORPHAN (references no open position) | **0** | every stop's pair has an open long |
| UNKNOWN | **0** | — |

- **Margin/funds locked by orphans: 0.00 USDT** (no orphans).
- Stop coverage per pair = **100%** (stop vol == position vol on every pair) → no over-coverage, so no
  stop can net the account short. Matches the bot's own reconcile line in-journal: `10 pairs · 25/25
  stops resting` (25 at that cycle; 28 now as ladder fills added lots).
- The 10 ladder-buy rungs are the continuous-ladder / seeder bids resting below market (post-only).

Raw order list (sorted pair, side):

| # | txid | pair | side | type | price | vol | lev | class |
|--:|---|---|---|---|--:|--:|---|---|
| 1 | OVSRZA-NOGJY-H75YD4 | ADA | buy | limit | 0.159874 | 320.0 | 10:1 | ladder-buy |
| 2 | O47GKI-2UATX-EPHCZN | ADA | sell | stop-loss | 0.148440 | 320.0 | 10:1 | stop-sell |
| 3 | OR2OM3-7TWJ3-5GCT7H | ADA | sell | stop-loss | 0.148440 | 320.0 | 10:1 | stop-sell |
| 4 | ONHZJ3-XR3MC-QUO6D5 | ADA | sell | stop-loss | 0.148440 | 320.0 | 10:1 | stop-sell |
| 5 | OIWFRJ-WCHJ5-QSTC4V | AVAX | buy | limit | 6.480 | 4.0 | 10:1 | ladder-buy |
| 6 | OKYS5O-JRZAC-KKRYYG | AVAX | sell | stop-loss | 6.020 | 8.0 | 10:1 | stop-sell |
| 7 | OPS7Y3-IFENB-BV6ADT | AVAX | sell | stop-loss | 6.020 | 8.0 | 10:1 | stop-sell |
| 8 | OYYEVY-4FH75-ONQPCM | AVAX | sell | stop-loss | 6.020 | 8.0 | 10:1 | stop-sell |
| 9 | OXQPA3-HOOAU-F5ZSUE | ETH | buy | limit | 1845.83 | 0.008 | 10:1 | ladder-buy |
| 10 | O4ZQUG-WPCNV-LI7H4F | ETH | sell | stop-loss | 1731.11 | 0.016 | 10:1 | stop-sell |
| 11 | O2FHJ2-772VK-JXNBUW | ETH | sell | stop-loss | 1731.11 | 0.016 | 10:1 | stop-sell |
| 12 | OMXM4E-74KW5-S4LUWR | ETH | sell | stop-loss | 1731.11 | 0.016 | 10:1 | stop-sell |
| 13 | ONB6EJ-TZTZF-Z3RP5M | ETH | sell | stop-loss | 1731.11 | 0.016 | 10:1 | stop-sell |
| 14 | O5U2ZU-XQIT3-4W6YNF | LINK | buy | limit | 8.28925 | 8.8 | 10:1 | ladder-buy |
| 15 | OU57WD-XMPUJ-NBNKZX | LINK | sell | stop-loss | 7.69639 | 8.8 | 10:1 | stop-sell |
| 16 | OBJHDB-VZQXD-TBGJM4 | LINK | sell | stop-loss | 7.69639 | 8.8 | 10:1 | stop-sell |
| 17 | ONEK73-YJFQQ-VWXCJP | LINK | sell | stop-loss | 7.69639 | 8.8 | 10:1 | stop-sell |
| 18 | OTBQXD-ETRF5-423EZP | LTC | buy | limit | 44.12 | 1.6 | 10:1 | ladder-buy |
| 19 | OV4L7L-ZDEZ4-AKHLC7 | LTC | sell | stop-loss | 41.14 | 1.6 | 10:1 | stop-sell |
| 20 | OEZGFU-LLYDU-ANOJ2T | LTC | sell | stop-loss | 41.14 | 1.6 | 10:1 | stop-sell |
| 21 | OJCS32-JHMYJ-GS46V7 | SOL | buy | limit | 75.13 | 0.96 | 10:1 | ladder-buy |
| 22 | OAFWRG-ZGDFN-VVKTIQ | SOL | sell | stop-loss | 69.76 | 0.96 | 10:1 | stop-sell |
| 23 | OI7ZBU-TTQG5-6K6V6A | SOL | sell | stop-loss | 69.76 | 0.96 | 10:1 | stop-sell |
| 24 | OIZ752-OEYH5-PFDSSP | SOL | sell | stop-loss | 69.76 | 0.96 | 10:1 | stop-sell |
| 25 | OOSE2D-7ZP6S-JPYHOI | SUI | buy | limit | 0.7332 | 80.0 | 10:1 | ladder-buy |
| 26 | O4S7PH-G4J5N-2B3IPJ | SUI | sell | stop-loss | 0.6808 | 80.0 | 10:1 | stop-sell |
| 27 | OSX67B-R5S6P-VDJBVQ | SUI | sell | stop-loss | 0.6808 | 80.0 | 10:1 | stop-sell |
| 28 | O24CVR-76MUT-5ANB42 | SUI | sell | stop-loss | 0.6808 | 80.0 | 10:1 | stop-sell |
| 29 | O7X7VG-KBWF5-XD66VP | XBT | buy | limit | 63506.2 | 0.0008 | 10:1 | ladder-buy |
| 30 | OCL3UO-EKUAN-HQD4PJ | XBT | sell | stop-loss | 58374.5 | 0.0008 | 10:1 | stop-sell |
| 31 | OR5DSQ-IFS7L-WVE2FM | XBT | sell | stop-loss | 58374.5 | 0.0008 | 10:1 | stop-sell |
| 32 | OGQL67-XG3IW-BMHJFB | XDG | buy | limit | 0.0724978 | 800.0 | 10:1 | ladder-buy |
| 33 | ONFZH4-VYBKI-UFEGGA | XDG | sell | stop-loss | 0.0666394 | 800.0 | 10:1 | stop-sell |
| 34 | O4F4W3-CEDHE-POILRJ | XDG | sell | stop-loss | 0.0666394 | 800.0 | 10:1 | stop-sell |
| 35 | O46NHC-5H6FS-EWXRM2 | XRP | buy | limit | 1.08189 | 26.4 | 10:1 | ladder-buy |
| 36 | O5QEJH-FUBBL-35UVEN | XRP | sell | stop-loss | 1.00452 | 26.4 | 10:1 | stop-sell |
| 37 | OJ5HJR-MZWDO-RKFTDD | XRP | sell | stop-loss | 1.00452 | 26.4 | 10:1 | stop-sell |
| 38 | OYXMAO-QPUHJ-OR2BWC | XRP | sell | stop-loss | 1.00452 | 26.4 | 10:1 | stop-sell |

---

## SECTION D — REALIZED P&L RECONCILIATION (last 24h)

Ledger balance trail is the ground truth. Window 2026-07-15 12:39 → 2026-07-16 12:39 MDT, 129 entries.

**Ledger by type (24h):**

| type | count | amount Σ | fee Σ |
|---|--:|--:|--:|
| deposit | 1 | +40.0000 | 1.7500 |
| margin (position-close settlements) | 41 | +5.1604 | 5.9343 |
| rollover (4h holding charges) | 87 | 0.0000 | 1.9539 |
| **NET** | **129** | **+45.1604** | **9.6382** |

**Balance trail:** first entry in window 176.5408 → last 212.0691 = **+35.5283**.
Back out the deposit (net +38.25 after its 1.75 fee): non-deposit balance change = **−2.72**.

**Itemized realized bleed (−2.72 USDT), by cause:**

| cause | amount | detail |
|---|--:|---|
| Net realized trading P&L on closed lots | **−0.77** | margin settlements +5.16 gross − 5.93 close/trade fees |
| Rollover / funding (4h margin holding) | **−1.95** | 87 rollover charges @ 0.01–0.05%/4h of notional |
| **Realized cash bleed (ex-deposit)** | **−2.72** | ties to balance trail exactly |
| *(separate)* deposit fee | −1.75 | cost of the +40 funding, not a trading loss |

**Stops fired? YES — 11 stop-fire sells in the 24h TradesHistory** (partial closes, not a T/P flatten):
XRP, SUI, SOL, LTC, LINK, ETH, XDG, XBT, AVAX, ADA×2. Example rows:

```
SELL XRPUSD:BTNL  vol 4.95    @ 1.1159    cost 5.5237   fee 0.0331
SELL SOLUSD:BTNL  vol 0.18    @ 77.48     cost 13.9464  fee 0.0837
SELL XBTUSD:BTNL  vol 0.00015 @ 64844.60  cost 9.7260   fee 0.0584
SELL ADAUSD:BTNL  vol 60.0    @ 0.164845  cost 9.8907   fee 0.0593   (×2)
```

These settled the 41 `margin`-type ledger closes. **Reconciliation to the exchange's −9.9 daily figure:**
the −9.9 the app shows is an **equity-based** day figure (realized + change in unrealized + fees), not
a cash figure. Cash realized was only **−2.72**; the rest of the −9.9 was **unrealized mark-to-market**
(open-position value falling, now sitting at `n` = −11.38). **Verdict on the "realized bleed":** it is
**small and fee-dominated** — the closes were roughly break-even on price (+5.16 gross) and the loss is
almost entirely **trading fees (5.93) + rollover (1.95)**. The visible daily pain is **unrealized**, not
realized. *(The dispatch's ~−2.3 realized estimate ≈ our −2.72; the ~0.4 gap is screenshot-vs-pull timing.)*

---

## SECTION E — TROUGH FORENSICS

**Trough located: equity 158.25 @ 2026-07-16 06:45:04 MDT** (equity_history, 281 samples/24h).
Baseline ~180 → 158.25 ≈ **−21.7 excursion**, matching the dispatch's −21.67. Recovered to 200.71 by
pull time. (A shallower earlier dip to ~160 sat 02:19–07:05 MDT.)

**Margin level at the trough: ~112–114%** (lowest parsed safety-event ML in the 00:00–14:00 window = 114%;
distribution shows repeated 111–112% prints). This is the "uncomfortably close" the operator saw — at
ml ~112%, the account was ~**5% of basket move** from the 80% margin-call line.

**What the bot did during the trough (journal, 12:00–13:30 UTC = 06:00–07:30 MDT):**

```
12:05:34Z order ADA  reladder: chain had no resting bid — re-placing rung below lowest fill 0.16
12:05:35Z order AVAX reladder: chain had no resting bid — re-placing rung below lowest fill 6.55
12:07:33Z recon      10 pairs · 25/25 stops resting
12:08:40Z order SOL  reladder: chain had no resting bid — re-placing rung below lowest fill 75.8
12:10:04Z order LTC  reladder: chain had no resting bid — re-placing rung below lowest fill 44.5
12:12:52Z safety     [margin-level] margin level 119% < stack floor 120% — seeds/rungs paused
   ... (this exact reladder+recon+safety triad repeats every ~10 min cycle for 90+ min) ...
```

Three things ran the whole time, and only these three:
1. **Stops stayed healthy** — every reconcile printed `25/25 stops resting`. No stop was lost. **No stop
   fired inside the trough window itself** (the 11 stop-fires were earlier / on the way down; the market
   recovered off 06:45 before more fired).
2. **Margin-level safety events fired continuously** — **531 safety events in 24h**, essentially all the
   same line: `margin level NNN% < stack floor — seeds/rungs paused` (floor shows 120 then 160 as the
   config was raised mid-window). This is high-frequency **alert fatigue**: one repeating "paused" line,
   not a distinct escalating "LIQUIDATION RISK" signal.
3. **Ladder re-place thrash** — for ADA/AVAX/SOL/LTC the journal logs `chain had no resting bid —
   re-placing rung` **every single cycle**, repeatedly, for the whole window. A successful placement
   would stop the "had no resting bid" message next cycle; its constant repetition means those 4 rungs
   **never rested** — they were being placed and immediately not-resting (post-only reject on the falling
   market and/or the stack-floor pause), a bounded but continuous churn.

**Net:** during a −21.7 equity excursion the bot took **no protective action beyond the stops already
resting**. It paused *new* stacking (correct), logged the same margin-level line hundreds of times, and
churned 4 ladder chains. It did **not** surface a single operator-facing "you are near liquidation" alert
distinct from the routine "seeds paused" noise — consistent with the operator's report that DEEPFIELD
emitted no warning.

---

## SECTION F — CONFIG DUMP (`deepfield/config.py`, live values)

| knob | value | notes |
|---|---|---|
| `EXEC_MODE` | **live** | env `DEEPFIELD_EXEC_MODE=live` (confirmed on PID 3320) |
| `PER_PAIR_LEVERAGE` | **10:1** (majors) / 5:1 / 2:1 | hardcoded per-pair max; all 10 live pairs = 10:1 |
| `EXEC_SIZE_MODE` | **min** | order vol = `SIZE_MULT × min_fill`, **not** equity-scaled |
| `SIZE_MULT` | **8** | raised 3→16 (07-15), cut 16→8 (07-16) after ml hit 106% |
| `RISK_PCT` | 0.02 | only used in `EXEC_SIZE_MODE="risk"` (inactive) |
| `EXEC_MAX_ORDER_NOTIONAL_USD` | 50.0 | per-order notional ceiling (scales with SIZE_MULT/conviction) |
| `LADDER_CONTINUOUS` / `LADDER_STEP_PCT` | True / 0.01 | next rung 1% below each fill, down to the stop |
| `SEED_PAIRS` | 10 majors | keep a working ladder chain per pair regardless of signal |
| `STOP_MODE` / band | support / 5–15% | **exchange-side** stop-loss **market** orders (`PROTECTIVE_STOP=True`) |
| `MARGIN_CAP_PCT` | 0.90 | one position ≤ 90% of free margin |
| `MAX_OPEN_POSITIONS` | 15 | **rail — DISABLED** (`RAILS_ENABLED=False`) |
| `RAILS_ENABLED` | **False** | drawdown kill-switch, daily/weekly loss caps, max-pos gate all OFF |
| `DAILY_LOSS_LIMIT_USD` / weekly | 15 / 35 | **inactive** (rails off) |
| `KILL_SWITCH_DD_PCT` | 0.20 | **inactive** (rails off) |
| `REALERT_HOURS` | 0 | per-symbol cooldown OFF → re-places on every close |
| `TP_ENABLED` / `TP_PCT` | True / 0.20 | flatten whole book at +20% equity, then restack |
| `MARGIN_LEVEL_ALERT_PCT` | 150 | fire safety alert below this ml |
| `MARGIN_LEVEL_STACK_FLOOR_PCT` | **160** | pause seeds/rungs below this ml (raised 120→160 on 07-16) |
| `RUNTIME_RECON_SECS` | 900 | re-reconcile ledger↔Kraken every 15 min |
| `ROLLOVER_POLL_SECS` | 3600 | account rollover-fee drag into meta hourly |
| `HALT_FILE` | `deepfield.HALT_ENTRIES` | manual operator kill-switch (touch to halt) |

**Hardcoded absolute-dollar values measured against a drifting balance** (the TrekBot epsilon-gate
pattern the dispatch asks about) — **present, and this is the structural smell:**
- `EXEC_MAX_ORDER_NOTIONAL_USD = 50.0` — absolute $, but self-scales with SIZE_MULT/conviction, so not a fixed trap.
- `DAILY_LOSS_LIMIT_USD = 15`, `WEEKLY_LOSS_LIMIT_USD = 35` — absolute $ (currently inert; rails off).
- `MARGIN_LEVEL_STACK_FLOOR_PCT` and `ALERT_PCT` are **ratios (%), not $** — good, they don't drift.
  But **sizing (`SIZE_MULT × min_fill`) is an absolute lot size that does NOT scale to equity** — so as
  equity changes, the book's *leverage* drifts. This is the mirror of the epsilon-gate: not a fixed
  dollar gate against a drifting balance, but a **fixed order size against a drifting equity**, which is
  why the same `SIZE_MULT` was "too hot" at 16 and got halved to 8 by hand.

---

## SECTION G — LIQ-AWARENESS CHECK

**Expected result confirmed: there is NO computation of liquidation price or a price-space margin buffer
anywhere in the codebase.** `grep -rniE "liquidat|liq_price|liqprice|liq_dist|maintenance.?margin|margin.?buffer"
deepfield/*.py` returns **only comments and log strings** — every hit is prose, none is arithmetic:

```
config.py:152   # ...liquidation is a non-issue...            (comment)
config.py:244   # liquidates from ml<=40% — bypassing every stop...   (comment)
executor.py:343 # force-liquidates from ml<=40%...             (docstring)
executor.py:362 f"seeds/rungs paused (Kraken liquidates at 40%)"      (log string)
executor.py:669/1326/1489/1499  "manual close / liquidation"  (comment/log)
```

**What the bot actually knows about margin:** exactly one quantity — `ml` (margin level %), **read
verbatim from Kraken's TradeBalance**, plus free margin. It is *not computed*; it is compared to two
thresholds (`MARGIN_LEVEL_ALERT_PCT=150`, `MARGIN_LEVEL_STACK_FLOOR_PCT=160`) at `executor.py:349–362`.

**What it does NOT compute, anywhere:** per-position liquidation price; account liquidation price;
distance-to-liquidation in price % (the −5.31% / −9.34% figures in Section B are computed *here*, by this
audit, not by the bot); maintenance-margin requirement; or any "you are X% of a move from a margin call"
signal. **Liquidation-buffer blindness = CONFIRMED.** The bot sees a lagging ratio (ml) after price has
already moved; it has no forward view of how far price can fall before the account is called or liquidated.

---

## OPEN QUESTIONS (for the architect — questions only)

1. **Leverage vs. stops.** With the book at 7.48× and margin-call at −5.31% but stops at ~−8%, Kraken
   calls the account *before* the stops fire. Is the intended primary exit the stops, or is the account
   relying on margin-level headroom the stops sit below?
2. **Force-liquidation vs. gap-through-stop.** Force-liq (−9.34%) sits just past the stop band (~−8%),
   and the stops are **market** orders that fill through a gap. In a fast gap-down, which resolves first —
   and is a ~1% price window between "all stops fill" and "account liquidates" an acceptable buffer?
3. **Sizing that ignores equity.** `min`-mode order size is fixed (`SIZE_MULT × min_fill`) and does not
   scale to balance, so the book's effective leverage drifts with equity and P&L. Should size be a
   function of equity/free-margin rather than a hand-tuned constant re-halved after each scare?
4. **Realized vs. unrealized framing.** Realized 24h cash bleed was only −2.72 (fee/rollover-dominated),
   while the −9.9 "daily" the operator reacted to is mostly unrealized mark-to-market. Should the
   operator-facing number separate realized cash from open-position mark-to-market?
5. **Rollover as a standing drag.** −1.95/24h in rollover on ~$1,500 notional (~0.13%/day, ~47%/yr
   annualized on notional) is a constant bleed the stack must out-earn. Is the ladder's expected edge
   sized against this carry?
6. **Alert signal-to-noise.** 531 identical "seeds/rungs paused" safety events in 24h — should the
   margin-level channel be de-duplicated and escalated into a distinct, throttle-surviving "near
   liquidation" alert, separate from the routine "paused" line the operator has learned to ignore?
7. **Liquidation blindness.** Given the API gives no per-position liq price, should the bot *derive*
   account liquidation distance (equity → ml=80%/40% → basket-move %) each cycle and alert on the
   *price-space* buffer, rather than only on the lagging ml ratio?
8. **Ladder re-place thrash.** ADA/AVAX/SOL/LTC logged "chain had no resting bid — re-placing rung"
   every cycle through the trough without the bid resting. Is the reladder net fighting the stack-floor
   pause (placing rungs the floor then blocks/rejects), and is that churn consuming rate-limit / attention?
9. **Deposit-fee leak.** The +40 top-up cost 1.75 (4.4%) in fees. Is manual instant-funding during a
   drawdown the intended cushion mechanism, or should deleverage (fewer/smaller lots) be the first lever?
10. **Timing discrepancy.** Ledger books the deposit at 12:01:52 MDT; the dispatch observed ~12:19.
    Is the ~18-minute gap just screenshot timing, or is there a clock/observation mismatch worth pinning?
