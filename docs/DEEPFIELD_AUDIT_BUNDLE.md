# ORACLE DEEPFIELD — Audit Bundle (single file)

> **How to use this file:** read Part 1 (design rationale + what to scrutinize),
> then audit the source in Part 2. Everything the review needs is in this one
> file — the live leveraged-execution path and every config knob it reads.
> Generated 2026-07-14 12:41 UTC from the running repo.
>
> **HISTORICAL SNAPSHOT — do not audit the current bot from this file.** It is
> preserved unedited as the record of what reviewers were given on 2026-07-14.
> The code has moved since; most importantly `RAILS_ENABLED` was re-armed to
> `True` on 2026-07-30 (`ad5097b`), so every statement below about absent circuit
> breakers, an unevaluated `MAX_OPEN_POSITIONS`, and unbounded position count is
> **false of the current tree**. Line numbers cited here have also drifted. For a
> current orientation read [`AUDIT_ORIENTATION.md`](AUDIT_ORIENTATION.md); for
> ground truth read `config.py` and `executor.rails_ok()`.

---

# PART 1 — DESIGN RATIONALE & AUDITOR INSTRUCTIONS

# ORACLE DEEPFIELD — Design Rationale & Intentional Overrides

**Audience:** an auditor doing a comprehensive review of the live execution code
(`deepfield/executor.py` and its call path).
**Purpose:** give you the design intent so that *deliberate* operator choices
aren't mistaken for bugs or oversights. This is orientation, **not** a request to
withhold findings. Several of the choices below are unusual for live-leveraged
code; where they are, that is flagged and **your scrutiny is explicitly invited** —
please judge whether the intentional design is itself dangerous. Nothing here
should be read as "safe, move on."

---

## 1. What the bot is

A two-part system on a single Kraken account:

1. **A cycle-bottom monitor** (signal-only). For 15 USD pairs it scores a
   7-signal "is this a macro bottom?" thesis on daily/weekly closed candles
   (`deepfield/signals.py`, `engine.py`). When a symbol reaches the required
   score it is a **confirmed BUY**. This half has no money in it and is not the
   audit target.

2. **A deterministic executor** (`deepfield/executor.py`). On a confirmed BUY it
   sizes an order, opens a **leveraged long on Kraken's `:BTNL` spot-margin
   book**, and rests a protective stop. There is **no learning/ML, no calibration
   period, no take-profit logic** — signal fires → size → open long → rest stop →
   record. That is the whole state machine.

The strategy is best described as **accumulate-the-bottom / pyramiding DCA**: it
only ever *buys* the thesis, it stacks additional entries while the thesis holds
(see §4), and it has no take-profit — exits are meant to be discretionary
(operator) with the exchange stop as a floor. Do not expect to find sell/TP logic;
its absence is by design, not a missing feature.

---

## 2. Execution pipeline (the money path)

```
confirmed BUY (candle close OR one-shot startup arm)
  └─ ingest._maybe_alert ─ engine.should_alert(REALERT_HOURS)   ← cooldown gate (OFF, see §3)
       └─ ingest._dispatch ─ executor.place_entry(symbol, price, card)
            ├─ portfolio_value()      live equity via broker.trade_balance()
            ├─ rails_ok(equity)       ← risk rails (auto-brakes OFF, see §3)
            ├─ compute_stop() + size()
            └─ AddOrder  post-only maker LIMIT, status='pending'   (NOT yet a position)
app loop, every cycle:
  executor.poll_fills()   pending → 'open' once Kraken confirms fill, THEN rest stop
on live restart:
  executor.verify_open_stops()   re-place a missing stop / cancel an orphaned one
```

Two properties here are carefully engineered and worth confirming rather than
assuming broken:

- **A resting entry limit is never treated as a position.** It is recorded
  `status='pending'`; only `poll_fills()` (executor.py:284) promotes it to `open`
  and rests the stop *after* Kraken reports executed volume. This is deliberate —
  resting a stop against an unfilled entry would open a naked short on the Non-ECP
  `:BTNL` book.
- **`verify_open_stops()` (executor.py:325) acts only on DEFINITE exchange state.**
  A transient API `None` is never read as "position gone" or "stop gone." The
  header comment spells out the two failure modes it is avoiding (abandoning a
  real long, or double-stopping into a short). If you're looking for a bug, this
  function's state-machine is the highest-value place to reason hard — but note it
  was written defensively on purpose.

---

## 3. Intentional operator overrides — do not mistake these for bugs

These are switched off/loosened **on purpose**, documented in-code at the config
line and in `docs/RULINGS.md`. Flagging "there are no circuit breakers" is
*expected* and correct as an observation — but please frame it as *"this is the
stated design; here is why it is/ isn't safe,"* because that is the real question.

| Override | Where | Effect | Why (operator) |
|---|---|---|---|
| **`RAILS_ENABLED = False`** | config.py:128; enforced executor.py:69 (`if not config.RAILS_ENABLED: return True`) | **Skips** the drawdown kill-switch, daily/weekly realized-loss caps, the **max-open-positions cap**, and the equity-unknown block. The bot never brakes *itself*. | "No circuit breakers, no fear." The bot should not halt its own accumulation during exactly the drawdowns it is designed to buy into. |
| **`REALERT_HOURS = 0`** | config.py:58; enforced via `engine.should_alert()` | Cooldown disabled → a symbol that stays BUY **re-fires an order on every daily/weekly close**. No separate dedupe. | Full pyramid/stack into a persistent bottom thesis. |
| **One-shot startup arm** | ingest.py:302 `_maybe_arm_startup_buy` | On each process start, every symbol *already* at BUY fires once on its first fresh tick. Re-arms every launch → **each restart re-fires every open BUY**. | Closes the gap where a restart would otherwise wait up to 7 days for the next close. |
| **`EXEC_SIZE_MODE = "min"`** | config.py:101; executor.py:137 | Sizes the **exchange minimum** order per pair (≥ ordermin and ≥ costmin), ignoring the 2%-risk math. | See §4 — this is the actual safety mechanism. |

**Retained on purpose (NOT removed):**
- **Manual `HALT_ENTRIES` file** (config.py:133; checked first in `rails_ok`,
  executor.py:67). This is the operator's hand-on-the-switch and is *always*
  honored regardless of `RAILS_ENABLED`. It is not a "circuit breaker."
- **Protective exchange stop** — see §5. Still enabled in code.

---

## 4. The strategy in plain terms — and the one fact that deserves the most scrutiny

Combine the three overrides above and the *effective* behavior is
**unbounded pyramiding**: `REALERT_HOURS=0` re-fires each close, the one-shot arm
re-fires every open BUY on each restart, and with `RAILS_ENABLED=False` the
`MAX_OPEN_POSITIONS` cap (config.py:129) is **never evaluated** — it sits behind
the `if not RAILS_ENABLED` early-return in `rails_ok()`. There is no other stacking
limit in the code.

**This is the single most audit-worthy fact in the codebase, and it is stated here
plainly on purpose.** The only thing bounding total exposure today is position
*size* (§5), not position *count*. Please evaluate it as such — do not let "it's
intentional" wave it past you.

---

## 5. What the safety model actually rests on (evaluate THIS)

The operator's containment argument is **not** "the rails are unnecessary." It is:

> **Min-sizing makes any single fill immaterial, so liquidation and per-trade loss
> are non-issues, so the self-braking rails are noise.**

That argument is only as strong as the mechanism behind it, so audit the mechanism:

1. **Min-sizing is a config knob, not an invariant.** Containment lives entirely in
   `EXEC_SIZE_MODE="min"` (config.py:101). Flip it to `"risk"` and sizing becomes
   `2% equity / stop-distance`, margin-capped at 90% (executor.py:148–177) — a
   materially different risk profile with the rails still off. The auditor should
   treat "min" as a **load-bearing setting**, and it's worth checking that `min`
   sizing is genuinely small for *every* pair (it depends on live
   `ordermin`/`costmin` from the `pairs` table, refreshed from AssetPairs — a stale
   or wrong row would change the floor).
2. **Protective stops ARE placed — be precise about this.** `PROTECTIVE_STOP=True`
   (config.py:111); `_rest_stop()` (executor.py:395) sends a real
   `stop-loss`/`trigger=index` sell after each confirmed fill, and
   `verify_open_stops()` re-places a missing one on restart. The runtime confirms
   it: two stops are currently resting. The operator's *view* is that stops
   "shouldn't even be needed" under min-sizing — but the **code as shipped places
   them**, so any statement to Fable that "there's no stop-loss" would be wrong and
   would fail a 30-second diff. Treat stops as present-and-active; the "optional"
   framing is philosophy, not code state.
3. **Order-path hardening that is real and should be verified, not assumed:**
   post-only maker entries only, no market orders (config.py:113); pending→open
   promotion gates positions/P&L/stops on *confirmed* fills; `AddOrder`/`CancelOrder`
   use non-idempotent transport (no blind resend — broker.py, `idempotent=False`);
   a **dedicated Kraken API key** with its own nonce line (broker.py header) to
   avoid nonce collisions with other bots. One real `EAPI:Invalid nonce` has
   occurred and self-healed via retry — worth confirming the retry path
   (broker.py:126) is correct under concurrency.

---

## 6. Where to point the audit

Genuinely valuable findings (these are *not* "intentional"):
- Correctness bugs in the `pending → open → stopped` state machine
  (`poll_fills`, `verify_open_stops`) — partial fills, restart races, orphan stops,
  the "definite state only" guarantee.
- Sizing/rounding errors that could make an order **larger** than intended
  (`size()`, `_min_volume`, lot/tick rounding) — this is the real blast-radius risk
  given min-sizing is the whole safety story.
- Nonce/retry/idempotency under concurrency (`broker.private`).
- Anything that could rest a stop against no position, or leave a filled position
  unprotected.
- The unbounded-pyramiding exposure of §4 — evaluated as a design risk, with the
  min-sizing mitigation of §5 weighed honestly.

Expected-and-intentional (flag as design observations, not defects): absence of
auto circuit breakers, absence of take-profit, re-firing on every close, re-firing
every open BUY on restart.

---

*If any statement here disagrees with the code, the code wins — tell us and we'll
fix the doc. This document is meant to make the audit sharper, not softer.*


---

# PART 2 — SOURCE UNDER AUDIT

Full, verbatim source of the money path. Primary target: `executor.py`.
Supporting files are included so every symbol referenced above resolves.

## `deepfield/executor.py` (1556 lines)

```python
"""Live Kraken spot-margin executor — deterministic. Operator override (RULINGS).

Confirmed BUY -> risk-rail gate -> size (2% equity risked, off the stop) -> open
a leveraged long on the :BTNL margin book -> rest a protective stop -> record.
No learning brain, no calibration period. EXEC_MODE gates everything:

  off      : no-op (default) — the ingest hook does nothing
  paper    : simulate the fill + stop, record to `orders`, zero network
  validate : send the REAL order with validate=true — Kraken checks pair name,
             leverage, precision, minimums and returns the parsed order WITHOUT
             executing. The proof gate before a cent is real.
  live     : real AddOrder + real resting stop

Mechanics (pair name, leverage, post-only, :BTNL trigger=index, price precision)
are hydra's field-verified patterns; nothing here is guessed.

AUDITOR NOTE: the auto-rails (drawdown kill-switch, loss caps, max-positions),
the alert cooldown, and re-fire-on-restart are switched OFF *by design* — see
docs/AUDIT_ORIENTATION.md before flagging removed safeguards. Containment rests
on min-sizing (EXEC_SIZE_MODE), NOT the rails; protective stops ARE placed
(PROTECTIVE_STOP=True). Read that doc for the intended model and the parts that
genuinely warrant scrutiny (sizing/rounding, the pending->open->stopped machine).
"""
import os
import json
import math
import time
import types
import logging
import datetime

from . import store
from . import broker
from . import config
from . import engine
from . import rest_client

log = logging.getLogger("deepfield.exec")


def _round_down(x, decimals):
    f = 10 ** decimals
    return math.floor(x * f) / f


def _round_price(x, decimals):
    return round(x, decimals)


# Kraken echoes a pair as a canonical key, an X-prefixed altname, or a base with a
# ':SUFFIX' (margin). Normalize all three to the canonical `rest` key so ledger rows,
# OpenPositions, and OpenOrders compare on ONE identity. (Hydra field-verified map.)
_ALT_TO_KEY = {"XBTUSD": "XXBTZUSD", "ETHUSD": "XETHZUSD",
               "XRPUSD": "XXRPZUSD", "LTCUSD": "XLTCZUSD"}


def _norm_pair_key(pr):
    base = str(pr or "").split(":")[0]
    if not base:
        return ""
    return base if base in {p["rest"] for p in config.PAIRS} else _ALT_TO_KEY.get(base, base)


def _age_secs(ts_iso):
    """Seconds since an ISO-8601 order timestamp; 0.0 (treated as never-stale) if the
    value is missing or unparseable, so a bad ts can never trigger a cancel."""
    try:
        t = datetime.datetime.fromisoformat(ts_iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def _entry_ttl_expired(ts_iso):
    return config.ENTRY_TTL_SECS > 0 and _age_secs(ts_iso) > config.ENTRY_TTL_SECS


def _new_userref():
    """Fresh Kraken client order id (positive int32). Random, not sequential — two
    processes/threads must never mint the same ref, and Kraken only needs it unique
    enough to search by (collisions across our own history are harmless: recovery
    matches OpenOrders/ClosedOrders, newest first, and refs live for minutes)."""
    import random
    return random.randint(1, 2**31 - 1)


# symbol -> public REST pair id, for the live-price clamp on ladder rungs.
_REST_PAIR = {p["ws"]: p["rest"] for p in config.PAIRS}

# Re-ladder backoff (module-level: an Executor is constructed per poll cycle, so
# per-instance state would reset every ~15s). A symbol whose re-place attempt did
# not produce a resting bid (ladder floor, own-level, regime gate, reject) is not
# retried for this long — keeps a chain holding at its floor from spamming.
_RELADDER_RETRY_SECS = 600
_reladder_next = {}     # symbol -> time.monotonic() of next allowed attempt
_seed_next = {}         # symbol -> time.monotonic() of next allowed seed attempt
# Runtime exchange-truth sweep cadence (audit 2026-07-13 #1): module-level because
# an Executor is constructed per poll cycle. 0.0 = run on the first live cycle so a
# stop that fired during a bot outage is caught minutes after boot, not next restart.
_recon_next = 0.0
# Ambiguous-AddOrder recovery: rows born from a network-unknown AddOrder carry a
# userref and txid NULL; give Kraken this long to show the order before concluding
# it never landed (poll cadence is ~15s, so this is ~20 attempts).
_USERREF_RESOLVE_SECS = 300


class Executor:
    def __init__(self, conn):
        self.conn = conn
        self.mode = config.EXEC_MODE

    def _journal(self, kind, symbol, text):
        """Isolated journal emit (v6 JOURNAL view). DISPLAY-ONLY narration — a
        failure here must NEVER delay or drop a fill/stop/order, so every emit
        goes through this try/except (same rule as the alerter dispatch fix).
        Never raises into the money path."""
        try:
            store.journal(self.conn, kind, symbol, text)
        except Exception:
            log.exception("journal emit failed (%s %s) — trade path unaffected", kind, symbol)

    def _safety(self, kind, symbol, text):
        """Safety event -> journal + operator alert (sound/notify/telegram, throttled
        in the alerter). Isolated — never raises into the money path. Lazy import
        keeps executor importable in test contexts without audio deps."""
        self._journal("safety", symbol or "", f"[{kind}] {text}")
        try:
            from . import alerter
            alerter.fire_safety(kind, symbol, text)
        except Exception:
            log.exception("safety alert failed (%s %s) — trade path unaffected", kind, symbol)

    # ── portfolio + rails ────────────────────────────────────────────────────

    def portfolio_value(self):
        if self.mode == "live":
            eq = broker.trade_balance()
            if eq is not None:
                self._update_peak(eq)
                return eq
            log.error("could not read live equity (TradeBalance) — cannot size")
            return None
        return config.PAPER_PORTFOLIO_USD

    def _update_peak(self, equity):
        try:
            peak = float(store.meta_get(self.conn, "peak_equity", 0) or 0)
        except (TypeError, ValueError):
            peak = 0.0
        if equity > peak:
            store.meta_set(self.conn, "peak_equity", equity)

    def rails_ok(self, equity):
        """Deterministic hard limits. (ok: bool, reason: str). The manual HALT file
        is always honored (operator's hand-on-switch); the AUTOMATIC circuit
        breakers below are gated by RAILS_ENABLED (operator override: default off)."""
        if os.path.exists(config.HALT_FILE):
            return False, f"HALT file present ({config.HALT_FILE})"
        if not config.RAILS_ENABLED:
            return True, "ok (auto-rails disabled)"
        # Fail-safe: in live mode an unknown equity means the kill-switch cannot be
        # evaluated — do NOT trade blind (min-size sizing ignores equity, so without
        # this the drawdown halt would be silently bypassed on a TradeBalance failure).
        if self.mode == "live" and equity is None:
            return False, "equity unavailable — cannot verify kill-switch (blocking)"
        # Cap counts committed exposure: filled positions AND resting entry limits
        # (a 'pending' limit will become a position — counting only 'open' lets many
        # rest under the cap and fill together, breaching MAX_OPEN_POSITIONS).
        n = store.committed_position_count(self.conn, mode=self.mode)
        if n >= config.MAX_OPEN_POSITIONS:
            return False, f"max open positions ({n}/{config.MAX_OPEN_POSITIONS})"
        try:
            peak = float(store.meta_get(self.conn, "peak_equity", 0) or 0)
        except (TypeError, ValueError):
            peak = 0.0
        if peak > 0 and equity is not None and equity < peak * (1 - config.KILL_SWITCH_DD_PCT):
            return False, (f"KILL SWITCH: equity ${equity:.2f} < {(1-config.KILL_SWITCH_DD_PCT)*100:.0f}% "
                           f"of peak ${peak:.2f} — manual reset (clear peak_equity)")
        now = datetime.datetime.now(datetime.timezone.utc)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        wk0 = (now - datetime.timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()
        dpl = store.realized_pnl_since(self.conn, day0)
        wpl = store.realized_pnl_since(self.conn, wk0)
        if dpl <= -config.DAILY_LOSS_LIMIT_USD:
            return False, f"daily loss limit (${dpl:.2f} <= -${config.DAILY_LOSS_LIMIT_USD})"
        if wpl <= -config.WEEKLY_LOSS_LIMIT_USD:
            return False, f"weekly loss limit (${wpl:.2f} <= -${config.WEEKLY_LOSS_LIMIT_USD})"
        return True, "ok"

    # ── sizing ───────────────────────────────────────────────────────────────

    def compute_stop(self, symbol, entry, card):
        """Stop price. STOP_MODE=support uses the 52w-low/W-support from the
        scorecard (bottom-thesis invalidation); clamped to [MIN,MAX]% of entry
        so it's never absurdly tight or wide."""
        support = getattr(card, "low_52w", None) if card is not None else None
        if config.STOP_MODE == "support" and support and 0 < support < entry:
            stop = support
        else:
            stop = entry * (1 - config.STOP_PCT)
        min_stop = entry * (1 - config.STOP_MAX_PCT)   # widest allowed (lowest price)
        max_stop = entry * (1 - config.STOP_MIN_PCT)   # tightest allowed (highest price)
        return max(min_stop, min(stop, max_stop))

    def _min_volume(self, ordermin, costmin, entry, lot_dec):
        """Smallest placeable order: >= ordermin AND cost >= costmin, on the lot
        grid (rounded UP so it never lands under either floor)."""
        need = max(ordermin, (costmin / entry) if entry > 0 else 0.0)
        if lot_dec is None:
            return need
        f = 10 ** lot_dec
        return math.ceil(need * f) / f

    def _owns_level_near(self, symbol, price, pct):
        """True if an OPEN position for symbol already sits within `pct` (fraction)
        of `price`. Used by continuous laddering to own each price level once
        instead of re-buying the same band as price churns."""
        if price <= 0:
            return False
        rows = self.conn.execute(
            "SELECT entry FROM orders WHERE symbol=? AND status='open' AND entry IS NOT NULL "
            "AND mode=?", (symbol, self.mode)).fetchall()
        return any(abs(e - price) <= pct * price for (e,) in rows if e)

    def size(self, symbol, entry, stop, leverage, equity, card=None):
        """Returns a sizing dict or None.
        EXEC_SIZE_MODE='min' (default): buy the minimum order — tiny, no liquidation
        worry. With a `card`, the min order is CONVICTION-WEIGHTED (Tier 1): the same
        engine.tranche the champion card displays scales the min-fill by score-over-
        required (delta 0 -> 1.0x STARTER, +1 -> 2.0x, +2 -> 3.0x; config.CONVICTION),
        so the live fill
        matches the shown tranche. No card -> flat 1.0x min (ladder/plan preview).
        'risk': volume = risk_usd/(entry-stop), margin-capped, min-floored."""
        info = store.get_pair_info(self.conn, symbol) or {}
        lot_dec = info.get("lot_decimals")
        ordermin = info.get("ordermin") or 0.0
        costmin = info.get("costmin") or 0.0
        stop_dist = entry - stop
        if entry <= 0:
            return None

        if config.EXEC_SIZE_MODE == "min":
            volume = self._min_volume(ordermin, costmin, entry, lot_dec)
            mult = 1.0
            if card is not None:
                # Conviction weighting: reuse the exact engine.tranche the display
                # uses so the fill == the shown qty. Guarded — conviction is a bonus
                # ON TOP of the min-fill floor, never a reason an order fails to size.
                try:
                    cvol, cmult = engine.tranche(card.score, card.required,
                                                 ordermin, costmin, lot_dec, entry)
                    if cvol > 0:
                        volume, mult = cvol, cmult
                except Exception:
                    log.warning("size %s: conviction tranche failed — flat min", symbol)
            # Operator size multiplier (2026-07-13): scale the (conviction-weighted)
            # min fill by SIZE_MULT, rounded DOWN to the lot grid but never below the
            # min-fill floor. Kept OUT of conviction_mult so the conviction-scaled
            # notional ceiling in the placement paths keeps its own meaning.
            smult = max(1.0, float(getattr(config, "SIZE_MULT", 1.0) or 1.0))
            if smult > 1.0 and volume > 0:
                scaled = volume * smult
                if lot_dec is not None:
                    scaled = _round_down(scaled, lot_dec)
                volume = max(volume, scaled)
            if volume <= 0:
                return None
            notional = volume * entry
            return {
                "volume": volume, "notional": notional, "margin": notional / leverage,
                "risk_usd": 0.0, "actual_risk": volume * max(0.0, stop_dist),
                "capped": False, "floored_to_min": mult == 1.0, "size_mode": "min",
                "conviction_mult": mult, "size_mult": smult,
            }

        # "risk" mode
        if stop_dist <= 0 or equity is None or equity <= 0:
            return None
        risk_usd = config.RISK_PCT * equity
        volume = risk_usd / stop_dist
        # margin cap: a single position posts at most MARGIN_CAP_PCT of equity
        max_margin = equity * config.MARGIN_CAP_PCT
        max_vol_by_margin = (max_margin * leverage) / entry
        capped = volume > max_vol_by_margin
        volume = min(volume, max_vol_by_margin)
        # Kraken floors
        floored_to_min = False
        min_vol = max(ordermin, (costmin / entry) if entry > 0 else 0.0)
        if volume < min_vol:
            volume = min_vol
            floored_to_min = True
        if lot_dec is not None:
            volume = _round_down(volume, lot_dec)
            if volume < min_vol:  # rounding pushed us under — bump one lot
                volume = _round_down(min_vol + (10 ** -lot_dec), lot_dec)
        if volume <= 0:
            return None
        notional = volume * entry
        margin = notional / leverage
        actual_risk = volume * stop_dist   # if floored/capped, real risk != target
        return {
            "volume": volume, "notional": notional, "margin": margin,
            "risk_usd": risk_usd, "actual_risk": actual_risk,
            "capped": capped, "floored_to_min": floored_to_min, "size_mode": "risk",
        }

    def plan(self, symbol, entry, card, equity):
        """Dry-run order plan for display — what live execution WOULD place, no
        order sent. Pure arithmetic + cached pair info. Returns dict or None."""
        if not entry or not equity or symbol not in config.MARGIN_PAIR:
            return None
        leverage = config.PER_PAIR_LEVERAGE.get(symbol)   # fixed, hardcoded
        if not leverage:
            return None
        stop = self.compute_stop(symbol, entry, card)
        s = self.size(symbol, entry, stop, leverage, equity, card=card)
        if not s:
            return None
        return {"leverage": leverage, "stop": stop, "entry": entry, **s}

    # ── placement ────────────────────────────────────────────────────────────

    def _accumulation_allowed(self):
        """FORK A regime gate (config.ACCUMULATE_ONLY_IN_BEAR): accumulate in weakness,
        pause in confirmed strength. Returns (ok, reason). FAILS OPEN — only an
        unambiguous BULL regime (config.NO_ACCUMULATE_REGIMES) pauses new entries/rungs;
        an unknown/missing/other regime still accumulates, so a stale or unavailable
        regime can never silently halt entries (operator no-blockers stance). The
        regime label is persisted to meta by ingest._recompute_regime."""
        if not getattr(config, "ACCUMULATE_ONLY_IN_BEAR", False):
            return True, ""
        regime = store.meta_get(self.conn, "regime", None)
        blocked = getattr(config, "NO_ACCUMULATE_REGIMES", ("BULL",))
        if regime and regime in blocked:
            return False, f"regime={regime} (accumulate only outside {tuple(blocked)})"
        return True, ""

    def _stack_margin_ok(self):
        """Margin-stack floor (audit 2026-07-13 #2): Kraken margin-calls at ml<=80%
        and force-liquidates from ml<=40% — bypassing every stop. Below
        config.MARGIN_LEVEL_STACK_FLOOR_PCT, SEEDS and LADDER RUNGS pause (they only
        GROW the book); confirmed-BUY signal entries are never gated here (operator
        no-blockers stance). Reads the margin level the app loop persisted to
        meta['web_live'] ≤15s ago — no extra API call. FAILS OPEN: a stale (>120s),
        missing, or unparseable level never pauses anything. Returns (ok, reason)."""
        floor = float(getattr(config, "MARGIN_LEVEL_STACK_FLOOR_PCT", 0) or 0)
        if floor <= 0:
            return True, ""
        try:
            blob = json.loads(store.meta_get(self.conn, "web_live") or "{}")
            lvl = blob.get("margin_level")
            updated = float(blob.get("updated", 0) or 0)
            if lvl is None or time.time() - updated > 120:
                return True, ""                    # unknown/stale — fail open
            lvl = float(lvl)
            if lvl < floor:
                self._safety("margin-level", "*",
                             f"margin level {lvl:.0f}% < stack floor {floor:.0f}% — "
                             f"seeds/rungs paused (Kraken liquidates at 40%)")
                return False, f"margin level {lvl:.0f}% < stack floor {floor:.0f}%"
            return True, ""
        except Exception:
            log.exception("stack margin check failed — failing open")
            return True, ""

    def place_entry(self, symbol, entry_price, card):
        if self.mode == "off":
            return None
        try:
            return self._place_entry(symbol, entry_price, card)
        except Exception:
            log.exception("executor.place_entry failed for %s (never kills the writer)", symbol)
            return None

    def _place_entry(self, symbol, entry_price, card):
        if symbol not in config.MARGIN_PAIR:
            log.error("no :BTNL margin pair for %s — cannot execute", symbol)
            return None
        ok_acc, why = self._accumulation_allowed()
        if not ok_acc:
            log.info("EXEC %s: accumulation paused (regime gate) — %s", symbol, why)
            return None
        equity = self.portfolio_value()
        ok, reason = self.rails_ok(equity)
        if not ok:
            log.warning("EXEC blocked for %s: %s", symbol, reason)
            return None
        leverage = config.PER_PAIR_LEVERAGE.get(symbol)
        if not leverage:
            log.error("no leverage for %s", symbol)
            return None
        stop = self.compute_stop(symbol, entry_price, card)
        sizing = self.size(symbol, entry_price, stop, leverage, equity, card=card)
        if sizing is None:
            log.warning("EXEC %s: sizing produced nothing (equity=%s)", symbol, equity)
            return None
        # Per-order notional ceiling (Finding 8): a checked bound on blast radius. A
        # valid min order is ~$3-8, so this only ever trips on a corrupt pairs row or a
        # flipped size mode producing an order orders-of-magnitude too large. Refuse it
        # (never halt the bot); the loud ERROR is the audit trail.
        # Conviction-SCALED (operator decision 2026-07-10): a legit Nx-conviction order
        # gets an Nx budget, so the steepened 2x/3x curve's strongest signals aren't
        # refused, while a corrupt row (orders of magnitude larger) still trips at every
        # tier. Guard on the BASE ceiling>0 so 0 still fully disables.
        cmult = sizing.get("conviction_mult", 1.0)
        # SIZE_MULT scales the notional too (audit 2026-07-13 M5): the operator's 3x
        # multiplier is a LEGITIMATE part of every order's size, so the sanity ceiling
        # must budget for it — otherwise a price rally silently trips the refuse path
        # on high-ordermin pairs (an unintended blocker). Corrupt-row protection is
        # preserved: the ceiling still catches orders orders-of-magnitude oversized.
        smult = max(1.0, float(sizing.get("size_mult", 1.0) or 1.0))
        ceiling = config.EXEC_MAX_ORDER_NOTIONAL_USD * cmult * smult
        if config.EXEC_MAX_ORDER_NOTIONAL_USD > 0 and sizing["notional"] > ceiling:
            log.error("EXEC %s REFUSED: order notional $%.2f exceeds %gx-conviction x %gx-size "
                      "ceiling $%.2f — not sending (sanity guard, not a rail; check the pairs "
                      "row / EXEC_SIZE_MODE)", symbol, sizing["notional"], cmult, smult, ceiling)
            return None

        margin_pair = config.MARGIN_PAIR[symbol]
        tick = config.MARGIN_TICK_DECIMALS.get(symbol, 2)
        if config.ENTRY_ORDERTYPE == "limit":
            # Post-only maker BUY must not cross the ask, or Kraken rejects it and
            # the entry silently never rests. Bid just below last so it always rests.
            px = _round_price(entry_price * (1 - config.POST_ONLY_SLIP_PCT), tick)
        else:
            px = _round_price(entry_price, tick)
        stop_px = _round_price(stop, tick)
        vol = sizing["volume"]
        tag = (f" ({cmult:g}x conviction)" if cmult > 1.0
               else " (FLOORED-min)" if sizing["floored_to_min"]
               else " (CAPPED)" if sizing["capped"] else "")
        log.info("EXEC %s [%s] %.6g @ %s x%d lev · stop %s · notional $%.2f margin $%.2f risk $%.2f%s",
                 symbol, self.mode, vol, px, leverage, stop_px, sizing["notional"],
                 sizing["margin"], sizing["actual_risk"], tag)

        params = {"pair": margin_pair, "type": "buy", "ordertype": config.ENTRY_ORDERTYPE,
                  "volume": str(vol), "leverage": str(leverage)}
        if config.ENTRY_ORDERTYPE == "limit":
            params["price"] = str(px)
            params["oflags"] = "post"

        row = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "symbol": symbol, "margin_pair": margin_pair, "side": "buy",
            "ordertype": config.ENTRY_ORDERTYPE, "mode": self.mode,
            "entry": px, "stop": stop_px, "volume": vol, "leverage": leverage,
            "notional": sizing["notional"], "margin": sizing["margin"],
            "risk_usd": sizing["actual_risk"],
            # Persist the entry conviction so continuous laddering can size each
            # rung the same (the fill->rung chain flows through the DB).
            "score": getattr(card, "score", None), "required": getattr(card, "required", None),
            "txid": None, "stop_txid": None,
            "status": "pending", "error": None,
            # Client order id (audit C3): sent on the live AddOrder so an order whose
            # transport failed AMBIGUOUSLY can be re-identified on Kraken instead of
            # becoming an untracked naked position. int32-positive per Kraken spec.
            "userref": _new_userref(),
        }

        if self.mode == "paper":
            row["txid"] = f"PAPER-{int(datetime.datetime.now().timestamp())}"
            row["status"] = "open"
            oid = store.insert_order(self.conn, row)
            self._rest_stop(symbol, margin_pair, stop_px, vol, leverage, oid, paper=True)
            return oid

        if self.mode == "validate":
            params["validate"] = "true"
            res = broker.private("/0/private/AddOrder", params)
            row["status"] = "validated" if res is not None else "rejected"
            row["error"] = None if res is not None else "validate returned None"
            if res is not None:
                row["txid"] = str((res.get("descr") or {}).get("order", "validated"))
            return store.insert_order(self.conn, row)

        # live: place the post-only maker limit and record it PENDING. A resting
        # limit is NOT a position — do not rest a stop yet (a stop with no position
        # would open a short) and do not count it as open. poll_fills() promotes
        # it to 'open' and rests the protective stop only once Kraken confirms fill.
        params["userref"] = str(row["userref"])
        tmeta = {}
        res = broker.private("/0/private/AddOrder", params, idempotent=False, meta=tmeta)
        if res and res.get("txid"):
            row["txid"] = res["txid"][0]
            row["status"] = "pending"
            log.info("ENTRY %s: limit resting @ %s (pending fill) %s", symbol, px, row["txid"])
            conv = f" · {cmult:g}x conviction" if cmult > 1.0 else ""
            self._journal("order", symbol, f"{row['volume']:g} entry limit resting @ {px} (pending){conv}")
            return store.insert_order(self.conn, row)
        if not tmeta.get("definite"):
            # Ambiguous transport (audit C3): the order MAY be on the book. Record it
            # pending with NO txid; poll_fills' userref recovery adopts it if it landed
            # or retires it 'rejected' once Kraken definitively shows nothing.
            row["status"] = "pending"
            row["error"] = "ambiguous AddOrder (network) — resolving by userref"
            log.warning("ENTRY %s: AddOrder transport ambiguous — recorded pending, "
                        "userref %s recovery will resolve", symbol, row["userref"])
            self._journal("order", symbol, f"ambiguous entry AddOrder — resolving by userref {row['userref']}")
            return store.insert_order(self.conn, row)
        row["status"] = "rejected"
        row["error"] = "no txid from AddOrder"
        return store.insert_order(self.conn, row)

    def poll_fills(self):
        """Promote resting entry limits to positions once Kraken confirms fill,
        then rest the protective stop. A limit sits 'pending' until this sees an
        executed volume — so pos counts, P&L, stops, and re-verification never
        touch an unfilled order. Terminal-but-unfilled orders become 'canceled'.
        LIVE only; paper simulates instant fill at placement."""
        if self.mode != "live":
            return
        # Re-protect BEFORE the T/P gate (audit 2026-07-13 M3): while an INCOMPLETE
        # flatten retries, _check_take_profit returns True every cycle — but by then
        # CancelOrderBatch has already swept the protective stops, so skipping the
        # reprotect pass here left blocked pairs naked for the whole retry period
        # (longest exactly when the Kraken API is degraded). Reprotect is cheap in
        # the healthy case (no API call when nothing is naked); the flatten simply
        # re-cancels anything it re-arms — churn, never nakedness.
        self._reprotect_naked_open()
        if self._check_take_profit():
            return          # book was just flattened — nothing to promote or ladder this cycle
        rows = self.conn.execute(
            "SELECT id, symbol, margin_pair, volume, leverage, stop, txid, ts, entry, score, required "
            "FROM orders WHERE status='pending' AND mode=?", (self.mode,)).fetchall()
        for oid, sym, mpair, vol, lev, stop, txid, ts, entry, score, required in rows:
            if not txid:
                # Ambiguous-AddOrder recovery (audit C3): this row was born from an
                # AddOrder whose network transport failed — it MAY be on the book.
                # Find it by our userref; adopt the txid if so, conclude 'rejected'
                # only after Kraken definitively shows nothing for the ref.
                txid = self._resolve_ambiguous_entry(oid, sym, ts)
                if not txid:
                    continue
            o = broker.query_order(txid)
            if o is None:
                continue                        # transient query failure — retry next cycle
            status = o.get("status")
            try:
                vol_exec = float(o.get("vol_exec", 0) or 0)
            except (TypeError, ValueError):
                vol_exec = 0.0
            if status not in ("closed", "canceled", "expired"):
                # Two reasons to act on a still-resting order: a PARTIAL fill (a real
                # position forming), or a stale UNFILLED bid past its TTL (Finding 5 —
                # else post-only bids pile up against Kraken's open-order cap). Both
                # cancel the order and resolve to its TERMINAL state before any DB
                # transition. Hazards (Finding 4): more can fill between the query and
                # the cancel landing, and the cancel itself can FAIL — so NEVER transition
                # until terminal with a settled volume (flipping to 'open' early would
                # size off a stale snapshot, or on a failed cancel orphan a still-resting
                # remainder — poll_fills only revisits 'pending'). On any failure/
                # uncertainty leave it pending and converge next cycle.
                if vol_exec > 0:
                    log.info("FILL %s: partial %.6g while resting — canceling remainder", sym, vol_exec)
                elif _entry_ttl_expired(ts):
                    log.info("EXPIRE %s: entry bid unfilled past TTL (%.0fs) — canceling stale post-only bid",
                             sym, _age_secs(ts))
                    self._journal("expire", sym, "entry bid unfilled past TTL — canceling")
                else:
                    continue                    # unfilled + resting, within TTL — patient bid
                if broker.cancel_order(txid) is None:
                    log.warning("FILL %s: cancel FAILED — leaving pending, retry next cycle", sym)
                    continue
                o = broker.query_order(txid)
                if o is None:
                    log.info("FILL %s: cancel sent — awaiting terminal confirm next cycle", sym)
                    continue
                status = o.get("status")
                if status not in ("closed", "canceled", "expired"):
                    log.info("FILL %s: cancel sent, order not terminal yet — retry next cycle", sym)
                    continue
                try:
                    vol_exec = float(o.get("vol_exec", 0) or 0)   # settled terminal volume
                except (TypeError, ValueError):
                    vol_exec = 0.0
                # fall through to the terminal handler with the settled status/vol_exec
            if vol_exec > 0:                     # terminal + filled (fully, or partial then done)
                self.conn.execute("UPDATE orders SET status='open', volume=? WHERE id=?", (vol_exec, oid))
                self.conn.commit()
                log.info("FILL %s: %.6g filled — position open, resting stop", sym, vol_exec)
                self._journal("fill", sym, f"{vol_exec:.6g} filled @ {lev}x — position open")
                self._rest_stop(sym, mpair, stop, vol_exec, lev, oid, paper=False)
                # Continuous laddering: protection is secured above; now drop the next
                # rung below the fill, sized at the SAME conviction the entry carried
                # (score/required ride down the chain). Isolated — a rung failure
                # never unwinds the fill.
                self._place_ladder_rung(sym, mpair, lev, stop, entry, score, required)
            else:
                self.conn.execute("UPDATE orders SET status='canceled', error=? WHERE id=?",
                                  (f"entry {status}, unfilled", oid))
                self.conn.commit()
                log.info("ENTRY %s: %s unfilled — no position", sym, status)
        # Runtime safety nets: re-protect any 'open' position whose stop-rest failed
        # (again — fresh fills this cycle can be naked; the pre-T/P pass above covers
        # the flatten-retry window), re-place the next ladder rung for any chain whose
        # resting bid died, then seed a starter chain on any SEED_PAIRS symbol with
        # nothing working.
        self._reprotect_naked_open()
        self._ensure_ladder_rungs()
        self._seed_chains()
        # Runtime exchange-truth sweep (audit 2026-07-13 #1): re-run the full
        # ledger↔Kraken reconcile on a timer so intraday stop-fires, force-
        # liquidations, and manual closes are noticed in minutes, not at restart.
        global _recon_next
        recon_secs = float(getattr(config, "RUNTIME_RECON_SECS", 0) or 0)
        if recon_secs > 0 and time.monotonic() >= _recon_next:
            _recon_next = time.monotonic() + recon_secs
            try:
                self.verify_open_stops(context="runtime")
            except Exception:
                log.exception("runtime reconcile sweep failed (poll_fills unaffected)")

    def _resolve_ambiguous_entry(self, oid, sym, ts):
        """A 'pending' row with NO txid came from an AddOrder whose transport failed
        ambiguously. Look it up by userref: found -> adopt the txid (the order IS
        ours, on the book or already terminal); definitively absent after the grace
        window -> 'rejected' (it never landed); API unknown -> retry next cycle.
        Returns the adopted txid or None."""
        row = self.conn.execute("SELECT userref FROM orders WHERE id=?", (oid,)).fetchone()
        userref = row[0] if row else None
        if not userref:
            # Legacy ambiguous row (pre-userref) — nothing to search by; age it out.
            if _age_secs(ts) > _USERREF_RESOLVE_SECS:
                self.conn.execute("UPDATE orders SET status='rejected', "
                                  "error='ambiguous AddOrder, no userref to recover by' WHERE id=?",
                                  (oid,))
                self.conn.commit()
            return None
        txid, od = broker.find_order_by_userref(userref)
        if txid:
            self.conn.execute("UPDATE orders SET txid=? WHERE id=?", (txid, oid))
            self.conn.commit()
            log.warning("RECOVER %s: ambiguous AddOrder found on Kraken by userref %s -> %s",
                        sym, userref, txid)
            self._journal("order", sym, f"recovered ambiguous entry by userref -> {txid}")
            return txid
        if od == "unknown":
            return None                          # API couldn't answer — retry next cycle
        if _age_secs(ts) > _USERREF_RESOLVE_SECS:
            self.conn.execute("UPDATE orders SET status='rejected', "
                              "error='AddOrder never landed (userref not found)' WHERE id=?", (oid,))
            self.conn.commit()
            log.info("RECOVER %s: userref %s definitively absent — AddOrder never landed", sym, userref)
        return None

    def _reprotect_naked_open(self):
        """Runtime safety net (gap B): a fill whose protective stop-rest FAILED
        (poll_fills commits 'open' then _rest_stop errors on a broker/API failure)
        stays status='open' with stop_txid NULL and is otherwise never revisited until
        a manual restart — a naked leveraged long. Re-rest it each poll cycle. Adopt a
        stop already resting on Kraken (persist-race orphan) before placing, so a retry
        never doubles the stop. Isolated — never raises into poll_fills. No API call in
        the healthy case (early-returns when nothing is naked)."""
        if self.mode != "live" or not config.PROTECTIVE_STOP:
            return
        try:
            naked = self.conn.execute(
                "SELECT id, symbol, margin_pair, volume, leverage, stop FROM orders "
                "WHERE status='open' AND stop_txid IS NULL AND mode=?", (self.mode,)).fetchall()
            if not naked:
                return
            kr_open_orders = broker.open_orders()
            claimed = {t for (t,) in self.conn.execute(
                "SELECT stop_txid FROM orders WHERE stop_txid IS NOT NULL")}
            # Backing check (audit 2026-07-13 C2): a naked row whose position has since
            # DIED (force-liquidation, manual close) must not get a fresh stop — that
            # MANUFACTURES the orphan stop that opens a short on trigger. Rest a stop
            # only when the pair's live long volume covers this row on top of the
            # volume its sibling rows' resting stops already commit. Definite state
            # only: OpenPositions None -> skip this cycle (retry ~15s), never guess.
            kr_pos = broker.open_positions()
            if kr_pos is None:
                log.warning("REPROTECT: OpenPositions unavailable — cannot verify backing, "
                            "retry next cycle")
                return
            positions = list(kr_pos.values()) if isinstance(kr_pos, dict) else []
            rest_by_ws = {p["ws"]: p["rest"] for p in config.PAIRS}

            def _pair_long_vol(sym):
                key = rest_by_ws.get(sym, "")
                tot = 0.0
                for p in positions:
                    if _norm_pair_key(p.get("pair", "")) != key:
                        continue
                    if str(p.get("type", "")).lower() == "sell":
                        continue
                    try:
                        tot += max(0.0, float(p.get("vol", 0) or 0) - float(p.get("vol_closed", 0) or 0))
                    except (TypeError, ValueError):
                        continue
                return tot

            stopped_vol = {}   # sym -> volume already committed by sibling resting stops
            for s, v in self.conn.execute(
                    "SELECT symbol, COALESCE(volume,0) FROM orders "
                    "WHERE status='open' AND stop_txid IS NOT NULL AND mode=?", (self.mode,)):
                stopped_vol[s] = stopped_vol.get(s, 0.0) + float(v or 0)
            for oid, sym, mpair, vol, lev, stop in naked:
                volf = float(vol or 0)
                free_backing = _pair_long_vol(sym) - stopped_vol.get(sym, 0.0)
                if free_backing < volf - 1e-8:
                    log.warning("REPROTECT %s: naked row %d NOT backed by live volume "
                                "(free %.8g < %.8g) — position likely gone; leaving for "
                                "the reconcile sweep to retire", sym, oid, free_backing, volf)
                    self._journal("stop", sym, f"naked row {oid} unbacked — reconcile sweep will retire it")
                    continue
                adopt = self._find_adoptable_stop(kr_open_orders, claimed, mpair, volf)
                if adopt:
                    self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?", (adopt, oid))
                    self.conn.commit()
                    claimed.add(adopt)
                    stopped_vol[sym] = stopped_vol.get(sym, 0.0) + volf
                    log.warning("REPROTECT %s: adopted resting orphan stop %s for naked open row %d",
                                sym, adopt, oid)
                    self._journal("stop", sym, f"reprotect: adopted resting stop {adopt}")
                    continue
                log.warning("REPROTECT %s: open row %d unprotected (stop-rest failed earlier) — "
                            "resting stop now", sym, oid)
                self._rest_stop(sym, mpair, stop, vol, lev, oid, paper=False)
                stopped_vol[sym] = stopped_vol.get(sym, 0.0) + volf
        except Exception:
            log.exception("reprotect pass failed (poll_fills unaffected)")

    def _live_last(self, symbol):
        """Last trade price from the public REST ticker, or None. Used only to keep
        a post-only rung below the live market — never for sizing or stops."""
        rp = _REST_PAIR.get(symbol)
        if not rp:
            return None
        try:
            res = rest_client.fetch_ticker([rp])
            if not res:
                return None
            t = next(iter(res.values()), None)
            return float(t["c"][0]) if t else None
        except Exception:
            log.exception("LADDER %s: live ticker fetch failed (rung falls back to "
                          "fill-anchored price)", symbol)
            return None

    def _ensure_ladder_rungs(self):
        """Runtime safety net (2026-07-12 offline-gap incident): the fill→rung
        accumulation chain dies SILENTLY when a resting rung goes terminal unfilled —
        a post-only kill on a gap-down (Kraken cancels the crossing maker, reason
        'Post only order') or the entry-TTL sweep — because nothing re-placed it; the
        pair then sat with open positions and ZERO resting bid until its next daily
        close (a 12h offline gap left every confirmed-BUY pair bidless in a falling
        market). Each poll cycle: any LIVE symbol with open rows but no pending entry
        gets its next rung re-placed, anchored one step below the LOWEST open fill
        (the chain's natural continuation; the live clamp in _place_ladder_rung keeps
        the maker resting after a further gap-down). Every rung guard still applies
        (HALT, regime gate, stop floor, own-level-once, rails). A symbol whose attempt
        doesn't rest a bid backs off _RELADDER_RETRY_SECS so a chain holding at its
        ladder floor stays quiet. No API call when every chain has a resting bid.
        Isolated — never raises into poll_fills."""
        if self.mode != "live" or not config.LADDER_CONTINUOUS:
            return
        try:
            rows = self.conn.execute(
                "SELECT symbol, margin_pair, entry, stop, leverage, score, required "
                "FROM orders WHERE status='open' AND mode=? AND entry IS NOT NULL "
                "ORDER BY symbol, entry ASC", (self.mode,)).fetchall()
            lowest = {}
            for sym, mpair, entry, stop, lev, score, required in rows:
                lowest.setdefault(sym, (mpair, entry, stop, lev, score, required))
            now = time.monotonic()
            for sym, (mpair, entry, stop, lev, score, required) in lowest.items():
                if store.has_pending_entry(self.conn, sym, mode=self.mode):
                    continue
                if now < _reladder_next.get(sym, 0.0):
                    continue
                _reladder_next[sym] = now + _RELADDER_RETRY_SECS
                log.info("RELADDER %s: open chain with no resting bid — re-placing "
                         "next rung below lowest open fill %s", sym, entry)
                self._journal("order", sym, f"reladder: chain had no resting bid — "
                                            f"re-placing rung below lowest fill {entry:g}")
                self._place_ladder_rung(sym, mpair, lev, stop, entry, score, required)
        except Exception:
            log.exception("reladder pass failed (poll_fills unaffected)")

    def _seed_chains(self):
        """Operator all-pairs directive (2026-07-13): every config.SEED_PAIRS symbol
        keeps a ladder chain WORKING at all times. A pair with NO open rows and NO
        resting bid gets a starter chain: a post-only bid just below live, through
        the exact _place_entry path a confirmed BUY uses (regime gate, HALT via
        rails_ok, min x SIZE_MULT sizing, pct-clamped stop — card=None so no
        conviction). This is also what RESTACKS the book after a T/P flatten.
        Backs off _RELADDER_RETRY_SECS per symbol so a rejecting pair stays quiet.
        Isolated — never raises into poll_fills."""
        if self.mode != "live" or not getattr(config, "SEED_PAIRS", ()):
            return
        # Margin-stack floor (audit #2): seeds GROW the book — below the floor, stop
        # growing (signal entries stay untouched; fails open on stale/unknown level).
        ok_ml, ml_why = self._stack_margin_ok()
        if not ok_ml:
            log.info("SEED: paused — %s", ml_why)
            return
        try:
            now = time.monotonic()
            for sym in config.SEED_PAIRS:
                if now < _seed_next.get(sym, 0.0):
                    continue
                if store.has_pending_entry(self.conn, sym, mode=self.mode):
                    continue
                if self.conn.execute("SELECT 1 FROM orders WHERE symbol=? AND status='open' "
                                     "AND mode=? LIMIT 1", (sym, self.mode)).fetchone():
                    continue
                _seed_next[sym] = now + _RELADDER_RETRY_SECS
                live = self._live_last(sym)
                if not live or live <= 0:
                    continue
                log.info("SEED %s: no chain working — starting ladder with a post-only "
                         "bid below live %s", sym, live)
                self._journal("order", sym, f"seed: starting chain below live {live:g}")
                self.place_entry(sym, live, card=None)
        except Exception:
            log.exception("seed pass failed (poll_fills unaffected)")

    # ── equity take-profit (operator 2026-07-13) ─────────────────────────────

    def _check_take_profit(self):
        """Stack, then flatten EVERYTHING at +TP_PCT over the armed baseline, then
        restack from the new base. The trigger rides the existing poll cycle; the
        closes are immediate market orders sized to Kraken's LIVE open volume at
        that instant — never a resting sell, so the no-net-short rule stands.
        Baseline lives in meta['tp_baseline']: armed at first sight of live equity,
        reset to post-flatten equity after each cycle (compounding). Returns True
        when a flatten ran (caller skips the rest of its cycle). Failure shape: an
        unavailable exchange read aborts BEFORE anything is touched and the trigger
        simply re-fires next poll; a partial flatten leaves unclosed rows protected
        (stops intact) or reprotectable (stop_txid NULL -> _reprotect_naked_open).
        Isolated — never raises into poll_fills."""
        if self.mode != "live" or not getattr(config, "TP_ENABLED", False):
            return False
        try:
            eq = self.portfolio_value()
            if eq is None:
                return False
            try:
                baseline = float(store.meta_get(self.conn, "tp_baseline", 0) or 0)
            except (TypeError, ValueError):
                baseline = 0.0
            if baseline <= 0:
                store.meta_set(self.conn, "tp_baseline", eq)
                log.warning("T/P ARMED: baseline $%.2f — flatten-everything target $%.2f (+%.0f%%)",
                            eq, eq * (1 + config.TP_PCT), config.TP_PCT * 100)
                self._journal("tp", "*", f"T/P armed: baseline ${eq:.2f}, "
                                         f"target ${eq * (1 + config.TP_PCT):.2f}")
                return False
            target = baseline * (1 + config.TP_PCT)
            if eq < target:
                return False
            log.warning("T/P HIT: equity $%.2f >= target $%.2f (baseline $%.2f) — "
                        "FLATTENING THE BOOK", eq, target, baseline)
            self._journal("tp", "*", f"T/P HIT: ${eq:.2f} >= ${target:.2f} — flattening")
            ran, complete = self._flatten_all()
            if not ran:
                return False        # exchange state unavailable — re-trigger next poll
            if not complete:
                # Baseline NOT reset: while equity holds over target the trigger
                # re-fires every poll and the flatten retries just what's left.
                # (Resetting here would strand the unclosed pairs until +TP_PCT
                # over the NEW baseline — they'd never flatten.)
                log.warning("T/P: flatten INCOMPLETE — baseline kept, retrying next poll")
                self._journal("tp", "*", "flatten incomplete — retrying next poll")
                return True
            new_eq = self.portfolio_value() or eq
            store.meta_set(self.conn, "tp_baseline", new_eq)
            log.warning("T/P CYCLE COMPLETE: banked vs baseline $%.2f -> new baseline $%.2f — "
                        "seeder restacks next cycle", baseline, new_eq)
            self._journal("tp", "*", f"cycle complete: new baseline ${new_eq:.2f} — restacking")
            return True
        except Exception:
            log.exception("take-profit check failed (poll_fills continues)")
            return False

    def _flatten_all(self):
        """Close the whole book NOW. Safety rule (verify_open_stops' rule): act only
        on DEFINITE exchange state, and never let a sell exceed live long volume.
        Sequence:
          1. OpenOrders (None -> abort untouched): cancel every resting entry bid,
             then every resting protective stop. A stop-cancel FAILURE marks its
             pair blocked (still protected, skipped this pass); a stop already off
             the book just gets its ledger reference cleared. Cleared/canceled
             stop_txids are NULLed + committed immediately, so a crash mid-flatten
             leaves rows that _reprotect_naked_open re-arms — protected, never naked.
          2. OpenPositions FRESH (after stop-cancels — with no sells resting, per-
             pair long volume can no longer shrink): market-close each unblocked
             pair's volume, rounded DOWN to the lot grid (an error leaves dust-LONG,
             never short). Sized from the EXCHANGE's volume, not the ledger's.
          3. Rows of a successfully-closed pair -> status='closed'.
        Returns (ran, complete): ran=False means exchange state was unavailable and
        nothing was recorded (retry next poll; a CancelAll may already have landed,
        in which case the stops-gone window lasts until a pass completes —
        tolerable seconds-to-minutes at +20% equity, and every later pass
        re-converges from definite state). complete=False means at least one pair
        is still open (blocked or a failed close) — the CALLER MUST KEEP THE
        BASELINE so the trigger re-fires and this retries what's left."""
        # 1) ONE targeted CancelOrderBatch sweeps every resting order WE placed — bids
        # and stops — instead of ~2N serial CancelOrder calls grinding the private-API
        # rate limiter. Targeted, NOT the account-wide CancelAll it used to be (audit
        # 2026-07-13 M4: CancelAll also swept manual/other-system orders, and stripped
        # the stop off any Kraken position the ledger had no row for — permanently
        # naked, since the flatten only closes DB-known pairs). Best-effort: the
        # reconcile below works from DEFINITE post-sweep state either way.
        ours = [t for (t,) in self.conn.execute(
            "SELECT txid FROM orders WHERE status='pending' AND txid IS NOT NULL AND mode=?",
            (self.mode,))]
        ours += [t for (t,) in self.conn.execute(
            "SELECT stop_txid FROM orders WHERE status='open' AND stop_txid IS NOT NULL "
            "AND mode=?", (self.mode,))]
        broker.cancel_order_batch(ours)
        oo = broker.open_orders()
        if oo is None:
            log.warning("T/P: OpenOrders unavailable after cancel sweep — cannot reconcile; "
                        "retry next poll")
            return False, False
        by_sym = {}
        rows = self.conn.execute(
            "SELECT id, symbol, margin_pair, leverage, stop_txid FROM orders "
            "WHERE status='open' AND mode=? ORDER BY symbol, id", (self.mode,)).fetchall()
        for oid, sym, mpair, lev, stop_txid in rows:
            d = by_sym.setdefault(sym, {"mpair": mpair, "lev": lev, "rows": [],
                                        "stops": set(), "blocked": False})
            d["rows"].append(oid)
            if stop_txid:
                d["stops"].add(stop_txid)
        # 1a) entry bids: resolve each to its TERMINAL state (batch query). A bid
        # that PARTIALLY filled before the sweep is a real long — promote it so its
        # pair's close (sized from live volume) retires it with the rest; leave
        # anything uncertain 'pending' and block its pair (poll_fills resolves it,
        # the trigger re-fires).
        pend = self.conn.execute(
            "SELECT id, symbol, margin_pair, leverage, txid FROM orders "
            "WHERE status='pending'").fetchall()
        terminal = broker.query_orders([t for (_, _, _, _, t) in pend if t])
        for oid, sym, mpair, lev, txid in pend:
            o = terminal.get(txid)
            status = (o or {}).get("status")
            if o is None or status not in ("closed", "canceled", "expired"):
                log.warning("T/P %s: bid %s not confirmed terminal — leaving pending, "
                            "blocking its pair this pass", sym, txid)
                by_sym.setdefault(sym, {"mpair": mpair, "lev": lev, "rows": [],
                                        "stops": set(), "blocked": False})["blocked"] = True
                continue
            try:
                vol_exec = float(o.get("vol_exec", 0) or 0)
            except (TypeError, ValueError):
                vol_exec = 0.0
            if vol_exec > 0:
                self.conn.execute("UPDATE orders SET status='open', volume=? WHERE id=?",
                                  (vol_exec, oid))
                by_sym.setdefault(sym, {"mpair": mpair, "lev": lev, "rows": [],
                                        "stops": set(), "blocked": False})["rows"].append(oid)
                log.info("T/P %s: bid partially filled %.6g before sweep — closing with the book",
                         sym, vol_exec)
            else:
                self.conn.execute("UPDATE orders SET status='canceled', error='tp-flatten' "
                                  "WHERE id=?", (oid,))
        self.conn.commit()
        # 1b) protective stops: CancelAll should have taken them all; anything still
        # resting gets an individual cancel (fail -> pair stays protected + blocked).
        # Cleared refs are NULLed + committed so a crash/failed close leaves rows
        # that _reprotect_naked_open re-arms.
        for sym, d in by_sym.items():
            for st in d["stops"]:
                if st in oo and broker.cancel_order(st) is None:
                    log.warning("T/P %s: stop %s survived the sweep and cancel FAILED — "
                                "pair stays protected, skipping its close this pass", sym, st)
                    d["blocked"] = True
                    continue
                self.conn.execute("UPDATE orders SET stop_txid=NULL WHERE stop_txid=?", (st,))
            self.conn.commit()
        # 2) fresh definite long volume per pair — no sells rest anymore, so this
        # can only be >= what the closes will find
        kr = broker.open_positions()
        if kr is None:
            log.warning("T/P: OpenPositions unavailable after stop-cancel — rows are "
                        "reprotectable; re-trigger next poll")
            return False, False
        positions = list(kr.values()) if isinstance(kr, dict) else []
        rest_by_ws = {p["ws"]: p["rest"] for p in config.PAIRS}

        def _long_vol(pos):
            if str(pos.get("type", "")).lower() == "sell":
                return 0.0
            try:
                return max(0.0, float(pos.get("vol", 0) or 0) - float(pos.get("vol_closed", 0) or 0))
            except (TypeError, ValueError):
                return 0.0

        # Shape sanity (verify_open_stops' rule): entries that parse to NO long
        # volume mean an unexpected response shape — retiring rows off it would
        # empty the ledger while real positions float. Bail; rows are already
        # reprotectable and the trigger re-fires.
        if kr and not any(_long_vol(p) > 0 for p in positions):
            log.warning("T/P: OpenPositions returned %d entries but no parseable long "
                        "volume — unexpected shape, aborting closes", len(kr))
            return False, False

        complete = True
        for sym, d in by_sym.items():
            if d["blocked"]:
                complete = False
                continue
            key = rest_by_ws.get(sym, "")
            vol = sum(_long_vol(p) for p in positions
                      if _norm_pair_key(p.get("pair", "")) == key)
            info = store.get_pair_info(self.conn, sym) or {}
            lot_dec = info.get("lot_decimals")
            if lot_dec is not None:
                vol = _round_down(vol, lot_dec)
            if vol <= 0:
                # exchange already flat for this pair (stops swept earlier) — retire rows
                for oid in d["rows"]:
                    self.conn.execute("UPDATE orders SET status='closed', "
                                      "error='tp-flatten (no live volume)' WHERE id=?", (oid,))
                self.conn.commit()
                log.info("T/P %s: no live volume — rows retired without a close", sym)
                continue
            volstr = f"{vol:.{lot_dec}f}" if lot_dec is not None else f"{vol:.8f}"
            params = {"pair": d["mpair"], "type": "sell", "ordertype": "market",
                      "volume": volstr, "leverage": str(d["lev"])}
            res = broker.private("/0/private/AddOrder", params, idempotent=False)
            if res and res.get("txid"):
                for oid in d["rows"]:
                    self.conn.execute("UPDATE orders SET status='closed', error='tp-flatten' "
                                      "WHERE id=?", (oid,))
                self.conn.commit()
                log.warning("T/P %s: market-closed %s (%s)", sym, volstr, res["txid"][0])
                self._journal("tp", sym, f"flatten: market-closed {volstr}")
            else:
                complete = False
                log.error("T/P %s: market close FAILED — rows stay open with stops cleared; "
                          "reprotect re-arms them next cycle, T/P re-fires while over target", sym)
        return True, complete

    def _place_ladder_rung(self, symbol, margin_pair, leverage, stop, filled_price,
                           score=None, required=None):
        """Continuous laddering (config.LADDER_CONTINUOUS): when a bid fills, drop the
        NEXT post-only rung one LADDER_STEP_PCT below the fill — CONVICTION-sized off
        the entry's score (a 7/7 position ladders 2x rungs; score/required ride down
        the chain via the DB), SAME support stop — so accumulation continues down
        toward the stop without waiting for a candle close or a restart. The conviction
        is FROZEN at the entry score for the whole descent (the weekly/daily signals
        don't move intraday; across a daily close they can, but the ladder does not
        re-score — a high-conviction entry keeps doubling down even if the signal later
        decays). Bounded by a NATURAL FLOOR: a rung at/under the stop is not placed.
        At most one LADDER rung per symbol — best-effort, NOT a hard invariant: the
        has_pending_entry check + insert below run on the poll_fills connection while a
        close-triggered _place_entry (which by design does NOT dedup — see the boot-arm
        note in ingest, close bids stack) can insert its own bid on the dispatch thread,
        so a close bid can coexist with a rung (and, in the microsecond check→insert
        window, a second rung). Benign: each bid carries its own txid + stop and
        reconciles independently; a shared lock would gain almost nothing since the
        entry path is unguarded anyway (operator no-blockers stance). Fully isolated —
        never raises into poll_fills, so the fill/stop just secured is never unwound.
        NULL score -> flat 1.0x min."""
        try:
            if not config.LADDER_CONTINUOUS or self.mode != "live":
                return
            if os.path.exists(config.HALT_FILE):
                log.info("LADDER %s: HALT present — no next rung", symbol)
                return
            ok_acc, why = self._accumulation_allowed()
            if not ok_acc:
                log.info("LADDER %s: accumulation paused (regime gate) — %s", symbol, why)
                return
            ok_ml, ml_why = self._stack_margin_ok()     # rungs grow the book (audit #2)
            if not ok_ml:
                log.info("LADDER %s: paused — %s", symbol, ml_why)
                return
            if not filled_price or filled_price <= 0:
                return
            if store.has_pending_entry(self.conn, symbol, mode=self.mode):
                return                                  # skip if a bid already rests (best-effort; see docstring)
            tick = config.MARGIN_TICK_DECIMALS.get(symbol, 2)
            target = _round_price(filled_price * (1 - config.LADDER_STEP_PCT), tick)
            # Post-only survivability (2026-07-12 offline-gap incident): target is
            # anchored to the FILL, which can be hours stale (offline gap, slow poll).
            # If price has since fallen to/below it, the maker bid crosses the book and
            # Kraken cancels it (reason 'Post only order') — silently killing the
            # accumulation chain. Clamp the rung below the LIVE last (same slip idiom
            # as _place_entry) so it always rests. Ticker unavailable -> keep the
            # fill-anchored target (old behavior); _ensure_ladder_rungs retries later.
            live = self._live_last(symbol)
            if live and live > 0:
                lid = _round_price(live * (1 - config.POST_ONLY_SLIP_PCT), tick)
                if target > lid:
                    log.info("LADDER %s: fill-anchored rung %s at/above live %s — "
                             "clamped to %s so the post-only maker rests",
                             symbol, target, live, lid)
                    target = lid
            if stop and target <= stop * (1 + config.LADDER_STOP_BUFFER):
                log.info("LADDER %s: next rung @ %s would hit stop %s — ladder floor reached, holding",
                         symbol, target, stop)
                return
            # Level dedupe: own each price level ONCE. In a choppy range the daily
            # re-arm + ladder would otherwise re-buy the same band (a fill dips 1%,
            # recovers, re-arms, dips again) and stack many overlapping rungs. Skip
            # the rung if we already hold an OPEN position within half a step of it —
            # descends cleanly toward the stop instead of churn-buying the band. The
            # just-filled anchor is a full step above target, so it never self-blocks.
            if self._owns_level_near(symbol, target, config.LADDER_STEP_PCT * 0.5):
                log.info("LADDER %s: already own a rung within half a step of %s — skip "
                         "(own each level once)", symbol, target)
                return
            equity = self.portfolio_value()
            ok, reason = self.rails_ok(equity)          # honor MAX_OPEN / drawdown / loss caps if on
            if not ok:
                log.info("LADDER %s: rails block next rung — %s", symbol, reason)
                return
            # Rebuild a stand-in card from the persisted entry conviction and size the
            # rung through the exact Tier-1 size(card=) path — so a rung is sized the
            # same as its entry would be. size() reads only .score/.required.
            conv = (types.SimpleNamespace(score=score, required=required)
                    if score is not None and required is not None else None)
            sizing = self.size(symbol, target, stop, leverage, equity, card=conv)
            if sizing is None:
                log.warning("LADDER %s: sizing produced nothing — no rung", symbol)
                return
            cmult = sizing.get("conviction_mult", 1.0)
            smult = max(1.0, float(sizing.get("size_mult", 1.0) or 1.0))
            ceiling = config.EXEC_MAX_ORDER_NOTIONAL_USD * cmult * smult   # conviction+size scaled (see _place_entry)
            if config.EXEC_MAX_ORDER_NOTIONAL_USD > 0 and sizing["notional"] > ceiling:
                log.error("LADDER %s REFUSED: rung notional $%.2f exceeds %gx-conviction x %gx-size "
                          "ceiling $%.2f", symbol, sizing["notional"], cmult, smult, ceiling)
                return
            vol = sizing["volume"]
            userref = _new_userref()
            params = {"pair": margin_pair, "type": "buy", "ordertype": "limit",
                      "volume": str(vol), "leverage": str(leverage),
                      "price": str(target), "oflags": "post",   # post-only can't fill instantly
                      "userref": str(userref)}
            tmeta = {}
            res = broker.private("/0/private/AddOrder", params, idempotent=False, meta=tmeta)
            row = {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "symbol": symbol, "margin_pair": margin_pair, "side": "buy",
                "ordertype": "limit", "mode": self.mode, "entry": target, "stop": stop,
                "volume": vol, "leverage": leverage, "notional": sizing["notional"],
                "margin": sizing["margin"], "risk_usd": sizing.get("actual_risk", 0.0),
                # carry the entry conviction to the next rung so it doesn't decay to 1x
                "score": score, "required": required,
                "txid": None, "stop_txid": None, "status": "pending", "error": None,
                "userref": userref,
            }
            if not (res and res.get("txid")):
                if not tmeta.get("definite"):
                    # Ambiguous transport (audit C3): the rung MAY be on the book — record
                    # it pending with no txid; the userref recovery adopts or retires it.
                    row["error"] = "ambiguous AddOrder (network) — resolving by userref"
                    store.insert_order(self.conn, row)
                    log.warning("LADDER %s: rung AddOrder transport ambiguous — recorded pending, "
                                "userref %s recovery will resolve", symbol, userref)
                    return
                log.warning("LADDER %s: next rung AddOrder returned no txid (post-only reject on a "
                            "gap-down, or transient) — reladder safety net retries in %ds",
                            symbol, _RELADDER_RETRY_SECS)
                return
            row["txid"] = res["txid"][0]
            store.insert_order(self.conn, row)
            conv_tag = f", {cmult:g}x conviction" if cmult > 1.0 else ""
            log.info("LADDER %s: next rung resting @ %s (%.6g%s) — one step below fill %s",
                     symbol, target, vol, conv_tag, filled_price)
            self._journal("order", symbol, f"ladder: next rung {vol:g} resting @ {target}{conv_tag}")
        except Exception:
            log.exception("LADDER %s: place next rung failed (fill/stop unaffected)", symbol)

    def _find_adoptable_stop(self, kr_open_orders, claimed, margin_pair, want_vol):
        """A protective stop that IS resting on Kraken's book for this pair but whose
        txid no ledger row tracks (a persist-race orphan: AddOrder landed, the DB write
        that records stop_txid then failed). ADOPT it instead of placing a duplicate —
        a doubled stop-sell, when triggered, opens a naked short (rank 3 / gap A).
        Matches a resting sell stop-loss on the same normalized pair, not already
        claimed by another row; prefers the closest volume match. Returns a txid or None
        (None also when OpenOrders was unavailable, i.e. kr_open_orders is falsy)."""
        if not kr_open_orders:
            return None
        target = _norm_pair_key(margin_pair)
        cands = []
        for txid, od in kr_open_orders.items():
            if txid in claimed:
                continue
            d = od.get("descr") or {}
            if str(d.get("type")).lower() != "sell" or "stop" not in str(d.get("ordertype", "")).lower():
                continue
            if _norm_pair_key(d.get("pair", "")) != target:
                continue
            try:
                ovol = float(od.get("vol", 0) or 0)
            except (TypeError, ValueError):
                ovol = 0.0
            cands.append((abs(ovol - want_vol), txid))
        if not cands:
            return None
        cands.sort()
        return cands[0][1]

    def verify_open_stops(self, context="startup"):
        """Reconcile each pair's OPEN ledger rows and their protective stops against
        Kraken's ACTUAL open long volume for that pair. Runs at live restart AND —
        audit 2026-07-13 #1 — on a runtime timer (context='runtime', every
        config.RUNTIME_RECON_SECS from poll_fills), so an intraday stop-fire,
        force-liquidation, or manual close is noticed in minutes, not at the next
        restart. Runtime findings additionally fire safety alerts.
        CRITICAL SAFETY RULE: act ONLY on DEFINITE exchange state. A transient API
        failure (None) is never treated as 'gone'.

        Why PER-PAIR VOLUME, not per-row pair-presence: the strategy stacks MANY
        rows per pair, so "does the pair have *any* position?" is the wrong test —
        a row whose OWN stop already triggered still sees a sibling position, would
        fall through to the re-place branch, and push total resting-stop volume
        ABOVE open volume. If the stops then sweep, the excess sell opens a SHORT on
        the Non-ECP :BTNL book (the exact catastrophe this function exists to
        prevent). Instead we budget each pair's DEFINITE open long volume across its
        open rows oldest-first: a row still backed by remaining volume keeps/gets
        exactly one resting stop; a row with no volume left behind it is closed and
        its stop (if any) canceled as an orphan. Invariant held: resting-stop volume
        per pair <= open volume per pair (never a naked short), while genuinely-open
        volume stays protected. Uncertainty (stop query None) still leaves the row
        untouched for the next restart."""
        if self.mode != "live":
            return
        pfx = context                                      # 'startup' | 'runtime' log tag
        kr = broker.open_positions()
        if kr is None:                                     # could not check -> do NOTHING
            log.warning("%s: OpenPositions unavailable — skipping stop verification", pfx)
            return

        # Kraken OpenPositions is posid -> {"pair": <key|altname>, "vol": str,
        # "vol_closed": str, "type": buy/sell} (hydra field-verified shape). Match on
        # the NORMALIZED pair key: drop any ':SUFFIX', map the four X-prefixed altnames
        # to their canonical key, then EXACT compare (substring matching would let an
        # empty/embedded name mis-match and mis-sum volume). Sum NET open LONG volume;
        # exclude a position only when it is EXPLICITLY typed 'sell' — a missing/odd
        # type still counts, so an unexpected response shape can never silently zero a
        # pair and strip real stops.
        rest_by_ws = {p["ws"]: p["rest"] for p in config.PAIRS}
        _norm_pair = _norm_pair_key   # module-level; shared with the stop-adopt helper

        def _long_vol(pos):
            if str(pos.get("type", "")).lower() == "sell":
                return 0.0
            try:
                return max(0.0, float(pos.get("vol", 0) or 0) - float(pos.get("vol_closed", 0) or 0))
            except (TypeError, ValueError):
                return 0.0

        positions = list(kr.values()) if isinstance(kr, dict) else []
        # Shape sanity: Kraken returned entries but NONE parse to a long volume -> the
        # response is not the shape we expect. Bail like 'could not check' rather than
        # read every row as unbacked and cancel real protective stops. (An empty dict
        # is the legitimate 'account flat' state and falls through to close rows.)
        if kr and not any(_long_vol(p) > 0 for p in positions):
            log.warning("%s: OpenPositions returned %d entries but no parseable long "
                        "volume — unexpected shape, skipping stop verification", pfx, len(kr))
            return

        def _pair_open_volume(sym):
            key = rest_by_ws.get(sym, "")
            if not key:
                return 0.0
            return sum(_long_vol(p) for p in positions if _norm_pair(p.get("pair", "")) == key)

        # Oldest-first: when part of a pair's stack has closed out, surviving open
        # volume is allocated to the earliest rows; the newest (now-unbacked) rows retire.
        rows = self.conn.execute(
            "SELECT id, symbol, margin_pair, volume, leverage, stop, stop_txid, txid "
            "FROM orders WHERE status='open' AND mode=? ORDER BY id", (self.mode,)).fetchall()

        # Batch every row's stop status into ONE QueryOrders sweep (50 txids/call)
        # instead of one call per row: a 60-position reconcile otherwise fires 60+
        # back-to-back QueryOrders and trips Kraken's private-API rate limit on every
        # restart. A stop_txid absent from this map reads as None below -> 'status
        # UNKNOWN', identical to the old per-row query_order failure (never re-placed).
        stop_info = broker.query_orders([r[6] for r in rows])   # r[6] = stop_txid
        # Kraken's actually-resting orders, so PASS 2 can ADOPT a stop that rests on the
        # book but whose txid the ledger lost (persist-race orphan) instead of placing a
        # duplicate -> naked short (rank 3 / gap A). None on API failure -> adoption is
        # skipped and we fall back to the (already UNKNOWN-guarded) re-place path.
        kr_open_orders = broker.open_orders()
        claimed_stops = {r[6] for r in rows if r[6]}   # stop txids already tracked by a row

        # PASS 1 — classify each row against its pair's DEFINITE open-volume budget and
        # do all REMOVALS now (close unbacked rows, cancel their orphan stops). Only
        # reductions here, so resting-stop volume can never transiently exceed open vol.
        budget = {}
        backed = []
        recon = {}   # per-pair happy-path tally -> a positive evidence line at the end
        for oid, sym, mpair, vol, lev, stop, stop_txid, entry_txid in rows:
            key = (sym, mpair)
            if key not in budget:
                budget[key] = _pair_open_volume(sym)
            if sym not in recon:
                recon[sym] = {"rows": 0, "openvol": budget[key], "closed": 0,
                              "resting": 0, "replaced": 0, "unknown": 0}
            recon[sym]["rows"] += 1
            try:
                volf = float(vol or 0)
            except (TypeError, ValueError):
                volf = 0.0
            o = stop_info.get(stop_txid) if stop_txid else None
            ostatus = (o or {}).get("status")
            # (rank 2) This row's OWN stop EXECUTED -> the position is definitively gone,
            # regardless of the pair's remaining open volume (which backs SURVIVING
            # siblings). Close it and record the stop-out P&L, but do NOT consume budget:
            # letting an executed-stop row eat a surviving sibling's backing is exactly
            # what made oldest-first budgeting cancel a live stop and re-place a stale
            # one against the wrong position. Handle this BEFORE the volume budget.
            if ostatus == "closed":
                pnl_json = self._stop_exit_pnl_json(sym, oid, entry_txid, o)
                self.conn.execute("UPDATE orders SET status='closed', error=COALESCE(?, error) WHERE id=?",
                                  (pnl_json, oid))
                self.conn.commit()
                recon[sym]["closed"] += 1
                recon[sym]["stop_fired"] = recon[sym].get("stop_fired", 0) + 1
                log.info("%s: %s order %d stop executed — closed w/ P&L, budget preserved for siblings",
                         pfx, sym, oid)
                self._journal("stop", sym, f"stop EXECUTED — row {oid} closed with P&L recorded")
                continue
            # No open volume left to back this row (and its stop did NOT execute) ->
            # position gone by manual close / liquidation. Its stop, if any, is now an
            # orphan (a stop-sell with no position opens a short). Close the row ONLY once
            # the orphan is PROVABLY neutralized; on cancel failure or UNKNOWN status leave
            # it 'open' and converge next restart — never strand a possibly-live stop by
            # closing its row (the row is then filtered out of every future reconcile).
            if budget[key] < volf - 1e-8:
                if ostatus in ("open", "pending"):
                    if broker.cancel_order(stop_txid) is None:
                        log.warning("%s: %s order %d unbacked but orphan-stop cancel FAILED — "
                                    "leaving open, retry next restart", pfx, sym, oid)
                        recon[sym]["unknown"] += 1
                        continue
                    log.warning("%s: %s order %d unbacked (pair open %.8g < %.8g) — "
                                "canceled orphan stop %s", pfx, sym, oid, budget[key], volf, stop_txid)
                    self._journal("stop", sym, f"canceled orphan stop {stop_txid} (row unbacked)")
                elif o is None and stop_txid:
                    log.warning("%s: %s order %d unbacked but stop status UNKNOWN — "
                                "leaving open, retry next restart", pfx, sym, oid)
                    recon[sym]["unknown"] += 1
                    continue
                # orphan gone (canceled/expired) or no stop_txid: nothing live to strand.
                self.conn.execute("UPDATE orders SET status='closed' WHERE id=?", (oid,))
                self.conn.commit()
                recon[sym]["closed"] += 1
                log.info("%s: %s order %d not backed by open volume — closed, no re-place", pfx, sym, oid)
                continue
            budget[key] -= volf                            # row consumes real open volume
            backed.append((oid, sym, mpair, vol, lev, stop, stop_txid))

        # Surplus check (audit 2026-07-13 M1): leftover pair budget after every row is
        # allocated = exchange volume NO ledger row tracks (an ambiguous AddOrder that
        # landed, a manual position, another system). It has NO stop under our control.
        # Silently discarding it was how 'all_ok' overstated coherence. Loud, and it
        # degrades the pair's ok flag; relative threshold so lot-dust never false-alarms.
        for (sym, _mp), leftover in budget.items():
            openvol = recon.get(sym, {}).get("openvol", 0.0) or 0.0
            if leftover > max(1e-8, 0.005 * openvol):
                recon[sym]["surplus"] = leftover
                log.error("%s: %s has %.8g open volume on Kraken NO ledger row tracks — "
                          "untracked position (no stop under our control)", pfx, sym, leftover)
                self._journal("recon", sym, f"UNTRACKED exchange volume {leftover:.8g} — no ledger row")
                self._safety("recon-mismatch", sym,
                             f"{leftover:.8g} open volume on Kraken has no ledger row (no stop)")
        # ... including pairs with NO ledger rows at all (an exchange position on a pair
        # the loop above never visited — the fully-invisible case) and pairs outside the
        # config universe entirely (ghost pairs — 6 were dropped this week).
        seen_keys = {rest_by_ws.get(s, "") for (s, _m) in budget}
        vol_by_key = {}
        for p in positions:
            k = _norm_pair(p.get("pair", ""))
            if k and k not in seen_keys:
                vol_by_key[k] = vol_by_key.get(k, 0.0) + _long_vol(p)
        ws_by_rest = {p["rest"]: p["ws"] for p in config.PAIRS}
        for k, v in vol_by_key.items():
            if v <= 1e-8:
                continue
            sym = ws_by_rest.get(k, k)              # ghost pairs keep the raw key
            log.error("%s: %s has %.8g open volume on Kraken with ZERO ledger rows — "
                      "fully untracked position", pfx, sym, v)
            self._journal("recon", sym, f"UNTRACKED position {v:.8g} — zero ledger rows")
            self._safety("recon-mismatch", sym,
                         f"{v:.8g} open volume on Kraken with zero ledger rows (no stop)")

        # PASS 2 — ADDITIONS only, after every removal is done: ensure each backed row
        # has exactly one resting stop; re-place only a DEFINITELY-gone/missing one.
        for oid, sym, mpair, vol, lev, stop, stop_txid in backed:
            o = stop_info.get(stop_txid) if stop_txid else None
            status = (o or {}).get("status")
            if status in ("open", "pending"):
                recon[sym]["resting"] += 1
                continue                                   # stop confirmed resting -> fine
            if o is None and stop_txid:
                # Stop status UNKNOWN (query failed) while backed: do NOT re-place
                # blindly (it might already rest -> duplicate -> short). Retry later.
                recon[sym]["unknown"] += 1
                log.warning("%s: %s order %d stop query failed — leaving as-is, retry next restart", pfx, sym, oid)
                continue
            if not config.PROTECTIVE_STOP:                 # stops disabled -> never place one
                continue
            # (rank 3 / gap A) Before placing, adopt a stop that IS resting on Kraken's
            # book for this pair but whose txid the ledger lost (persist-race orphan) —
            # placing a duplicate would double the stop-sell -> naked short when hit.
            adopt = self._find_adoptable_stop(kr_open_orders, claimed_stops, mpair, float(vol or 0))
            if adopt:
                self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?", (adopt, oid))
                self.conn.commit()
                claimed_stops.add(adopt)
                recon[sym]["resting"] += 1
                log.warning("%s: %s order %d adopted resting orphan stop %s (ledger had %s) — "
                            "no duplicate placed", pfx, sym, oid, adopt, stop_txid or "none")
                self._journal("stop", sym, f"adopted resting orphan stop {adopt}")
                continue
            # Stop DEFINITELY gone (closed/canceled/expired) or never placed, no orphan to
            # adopt, and the position is backed: re-place once, non-idempotent transport.
            log.warning("%s: %s order %d position backed but stop %s — re-placing",
                        pfx, sym, oid, status or "missing")
            # Stale-price tell (audit M4): if the market gapped BELOW the stored stop
            # while it was off the book, this stop-loss sell triggers the moment it
            # rests — that is the stop honoring itself LATE (correct for a long under
            # its invalidation), but say so loudly instead of letting it read as a
            # surprise market close.
            live = self._live_last(sym)
            if live and stop and live <= float(stop):
                log.warning("%s: %s re-placing stop %s AT/ABOVE live %s — it will trigger "
                            "immediately (late stop honor)", pfx, sym, stop, live)
                self._journal("stop", sym, f"stop {stop} at/above live {live} — will trigger on rest")
            res = broker.private("/0/private/AddOrder", {
                "pair": mpair, "type": "sell", "ordertype": "stop-loss",
                "price": str(stop), "volume": str(vol), "leverage": str(lev), "trigger": "index"},
                idempotent=False)
            if res and res.get("txid"):
                self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?", (res["txid"][0], oid))
                self.conn.commit()
                claimed_stops.add(res["txid"][0])   # a sibling row must not adopt it
                recon[sym]["replaced"] += 1
                log.info("PROTECT %s: re-placed stop @ %s (%s)", sym, stop, res["txid"][0])
                self._journal("stop", sym, f"re-placed missing stop @ {stop}")
            else:
                log.error("PROTECT %s: re-place FAILED — position may be UNPROTECTED", sym)
                self._journal("stop", sym, "re-place FAILED — position may be UNPROTECTED")
                self._safety("unprotected", sym, "stop re-place FAILED — position may be UNPROTECTED")

        # Positive evidence: one line per pair, so a clean reconcile leaves proof it ran
        # and the invariant held — not silence to interpret (audit re-review). E.g.
        # "reconcile SUI/USD: 3 open rows, 15 open vol on Kraken, 3 stops resting, ...".
        for sym, r in recon.items():
            log.info("reconcile %s: %d open rows, %.6g open vol on Kraken, %d stops resting, "
                     "%d closed, %d re-placed, %d unknown",
                     sym, r["rows"], r["openvol"], r["resting"], r["closed"], r["replaced"], r["unknown"])

        # v6 SURVEY: publish this reconcile as display-truth for the header/BOOK
        # coherence readout. A pair is 'ok' iff no stop status was left UNKNOWN
        # (a query failure that could hide an unprotected row) AND no untracked
        # surplus volume was found (audit M1 — surplus used to pass silently).
        # Since audit 2026-07-13 #1 this runs on the RUNTIME_RECON_SECS timer too,
        # so the stamp is minutes old at most, not boot-old.
        try:
            per_pair = {sym: {"rows": r["rows"], "vol": round(r["openvol"], 8),
                              "stops": r["resting"] + r["replaced"],
                              "ok": r["unknown"] == 0 and not r.get("surplus")}
                        for sym, r in recon.items()}
            payload = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "per_pair": per_pair, "all_ok": all(p["ok"] for p in per_pair.values()),
                       "context": context}
            store.meta_set(self.conn, "last_recon", json.dumps(payload))
        except Exception:
            log.exception("last_recon publish failed (reconcile itself unaffected)")
        total_rows = sum(r["rows"] for r in recon.values())
        total_stops = sum(r["resting"] + r["replaced"] for r in recon.values())
        self._journal("recon", "", f"{len(recon)} pairs · {total_stops}/{total_rows} stops resting")
        # Runtime findings page the operator (audit #1/#3): a stop-fire cluster, an
        # unbacked-row retirement, or a re-placed stop found MID-SESSION is exactly
        # what used to stay invisible until restart.
        if context == "runtime":
            fired = {s: r["stop_fired"] for s, r in recon.items() if r.get("stop_fired")}
            if fired:
                detail = ", ".join(f"{s}×{n}" for s, n in fired.items())
                self._safety("stop-fired", "*", f"protective stops EXECUTED: {detail} — "
                                                f"rows closed with P&L recorded")
            closed_other = sum(r["closed"] - r.get("stop_fired", 0) for r in recon.values())
            if closed_other > 0:
                self._safety("recon-mismatch", "*",
                             f"{closed_other} ledger row(s) retired — position gone without "
                             f"our stop (manual close / liquidation)")
            replaced = sum(r["replaced"] for r in recon.values())
            if replaced > 0:
                self._safety("unprotected", "*",
                             f"{replaced} missing protective stop(s) re-placed mid-session")

    def _stop_exit_pnl_json(self, sym, oid, entry_txid, stop_order):
        """Realized P&L for a STOP-triggered close, from Kraken's own execution records:
        proceeds (stop-sell cost - fee) minus cost basis (entry-buy cost + fee). Returns a
        JSON string {'pnl','exit','closed_ts'} or None. None when the exit isn't a settled
        stop (manual close / liquidation / query failure) — those stay unrecorded and
        realized_pnl_since ignores them; loss caps care about stop-outs, so this is aligned.
        LIMITATION: rollover/financing fees are NOT included, so a held leveraged loss is
        slightly understated (the cap trips marginally late). Best-effort: never raises
        into the close path."""
        try:
            if not (stop_order and stop_order.get("status") == "closed"):
                return None
            s_cost = float(stop_order.get("cost", 0) or 0)
            s_fee = float(stop_order.get("fee", 0) or 0)
            if float(stop_order.get("vol_exec", 0) or 0) <= 0 or s_cost <= 0:
                return None
            eo = broker.query_order(entry_txid) if entry_txid else None
            if not eo:
                return None
            e_cost = float(eo.get("cost", 0) or 0)
            e_fee = float(eo.get("fee", 0) or 0)
            if e_cost <= 0:
                return None
            pnl = (s_cost - s_fee) - (e_cost + e_fee)
            # Bucket by the stop's ACTUAL execution time (Kraken 'closetm'), not restart
            # 'now' — a stop that triggered on a prior day/week must land in THAT window's
            # realized-loss cap, not the restart moment (rank 8; mirrors F6's closed_ts
            # bucketing intent). Fall back to now only when closetm is absent/unparseable.
            ct = stop_order.get("closetm")
            try:
                closed_ts = (datetime.datetime.fromtimestamp(float(ct), datetime.timezone.utc).isoformat()
                             if ct else datetime.datetime.now(datetime.timezone.utc).isoformat())
            except (TypeError, ValueError):
                closed_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            log.info("PNL %s: order %d realized $%.4f (stop exit)", sym, oid, pnl)
            return json.dumps({"pnl": round(pnl, 8), "exit": "stop", "closed_ts": closed_ts})
        except Exception:
            log.exception("PNL record failed for order %d (closing anyway)", oid)
            return None

    def _rest_stop(self, symbol, margin_pair, stop_px, volume, leverage, order_id, paper):
        if not config.PROTECTIVE_STOP:
            return
        if paper:
            self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?",
                              (f"PAPER-STOP-{order_id}", order_id))
            self.conn.commit()
            return
        params = {"pair": margin_pair, "type": "sell", "ordertype": "stop-loss",
                  "price": str(stop_px), "volume": str(volume),
                  "leverage": str(leverage), "trigger": "index"}  # :BTNL rejects 'last'
        res = broker.private("/0/private/AddOrder", params, idempotent=False)
        if res and res.get("txid"):
            self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?", (res["txid"][0], order_id))
            self.conn.commit()
            log.info("PROTECT %s: exchange stop @ %s (%s)", symbol, stop_px, res["txid"][0])
            self._journal("stop", symbol, f"protective stop rested @ {stop_px}")
        else:
            log.error("PROTECT %s: FAILED to rest stop — position is UNPROTECTED", symbol)
            self._journal("stop", symbol, "STOP FAILED — position UNPROTECTED")
            self._safety("unprotected", symbol,
                         "stop-rest FAILED — naked leveraged long (reprotect retries each cycle)")
```

## `deepfield/config.py` (325 lines)

```python
"""Operator-edited CONFIG BLOCK (v4.4 'edit these freely' ethos). SPEC §10.

Runtime truth for ordermin/costmin/lot_decimals is the `pairs` table, refreshed
from AssetPairs at startup + daily. The numbers below are SEED/FALLBACK only —
never trusted as truth (SPEC §7 F8, Appendix C).
"""
import os
import logging

_log = logging.getLogger(__name__)

# Paths (single 916G root disk; project island under home). RULINGS env ruling.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_PKG_DIR)
DB_PATH = os.path.join(PROJECT_ROOT, "deepfield.db")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")

# Backfill/live intervals (minutes). SPEC §6.
INTERVALS = (1440, 10080)

# v1 asset code -> v2 symbol normalization (the rename traps). SPEC §6.
NORMALIZE = {"XBT": "BTC", "XDG": "DOGE"}

# --- Pairs (Appendix C). ordermin/costmin live-verified 2026-07-03. ---
# ws_symbol is derived from wsname via NORMALIZE at runtime; precomputed here.
PAIRS = [
    # rest,        wsname,      ws,          display, ordermin,  costmin
    {"rest": "XXBTZUSD", "wsname": "XBT/USD",  "ws": "BTC/USD",  "display": "BTC",  "ordermin": 0.00005, "costmin": 0.5},
    {"rest": "XETHZUSD", "wsname": "ETH/USD",  "ws": "ETH/USD",  "display": "ETH",  "ordermin": 0.001,   "costmin": 0.5},
    {"rest": "XXRPZUSD", "wsname": "XRP/USD",  "ws": "XRP/USD",  "display": "XRP",  "ordermin": 1.65,    "costmin": 0.5},
    {"rest": "SOLUSD",   "wsname": "SOL/USD",  "ws": "SOL/USD",  "display": "SOL",  "ordermin": 0.06,    "costmin": 0.5},
    {"rest": "SUIUSD",   "wsname": "SUI/USD",  "ws": "SUI/USD",  "display": "SUI",  "ordermin": 5,       "costmin": 0.5},
    {"rest": "XDGUSD",   "wsname": "XDG/USD",  "ws": "DOGE/USD", "display": "DOGE", "ordermin": 50,      "costmin": 0.5},
    {"rest": "XLTCZUSD", "wsname": "LTC/USD",  "ws": "LTC/USD",  "display": "LTC",  "ordermin": 0.1,     "costmin": 0.5},
    {"rest": "LINKUSD",  "wsname": "LINK/USD", "ws": "LINK/USD", "display": "LINK", "ordermin": 0.55,    "costmin": 0.5},
    {"rest": "ADAUSD",   "wsname": "ADA/USD",  "ws": "ADA/USD",  "display": "ADA",  "ordermin": 20,      "costmin": 0.5},
    {"rest": "AVAXUSD",  "wsname": "AVAX/USD", "ws": "AVAX/USD", "display": "AVAX", "ordermin": 0.5,     "costmin": 0.5},
    {"rest": "AAVEUSD",  "wsname": "AAVE/USD", "ws": "AAVE/USD", "display": "AAVE", "ordermin": 0.05,    "costmin": 0.5},
    {"rest": "UNIUSD",   "wsname": "UNI/USD",  "ws": "UNI/USD",  "display": "UNI",  "ordermin": 1.5,     "costmin": 0.5},
    {"rest": "DOTUSD",   "wsname": "DOT/USD",  "ws": "DOT/USD",  "display": "DOT",  "ordermin": 3.9,     "costmin": 0.5},
    {"rest": "BCHUSD",   "wsname": "BCH/USD",  "ws": "BCH/USD",  "display": "BCH",  "ordermin": 0.01,    "costmin": 0.5},
    {"rest": "ALGOUSD",  "wsname": "ALGO/USD", "ws": "ALGO/USD", "display": "ALGO", "ordermin": 41,      "costmin": 0.5},
    # 5x margin expansion (operator 2026-07-13 "add all of the 5x"). ordermin/costmin
    # from Kraken AssetPairs; each margin-enabled @ 5x, validated via --exec-probe.
    # NOT in SEED_PAIRS (5x pairs eat 2x the margin/notional — trade on confirmed BUYs
    # only, no auto-seeded chains). PAXG is a gold-backed token (tracks spot gold, not
    # a crypto cycle) — added then PULLED by operator 2026-07-13 (gold, not a cycle).
    # DROPPED — no :BTNL margin book on this account (EQuery:Unknown asset pair on the
    # validate probe, despite AssetPairs listing leverage_buy): BNB, FARTCOIN, TAO,
    # XAUT, XMR. Also HYPE: validates, but only ~25 weekly candles (2024 listing) — too
    # young for the weekly thesis, sits permanently NA, and tripped the parity gate.
    {"rest": "CRVUSD",   "wsname": "CRV/USD",  "ws": "CRV/USD",  "display": "CRV",  "ordermin": 20,      "costmin": 0.5},
    {"rest": "HBARUSD",  "wsname": "HBAR/USD", "ws": "HBAR/USD", "display": "HBAR", "ordermin": 55,      "costmin": 0.5},
    {"rest": "PEPEUSD",  "wsname": "PEPE/USD", "ws": "PEPE/USD", "display": "PEPE", "ordermin": 1500000, "costmin": 0.5},
    {"rest": "SHIBUSD",  "wsname": "SHIB/USD", "ws": "SHIB/USD", "display": "SHIB", "ordermin": 770000,  "costmin": 0.5},
    {"rest": "TRXUSD",   "wsname": "TRX/USD",  "ws": "TRX/USD",  "display": "TRX",  "ordermin": 16,      "costmin": 0.5},
    {"rest": "ZECUSD",   "wsname": "ZEC/USD",  "ws": "ZEC/USD",  "display": "ZEC",  "ordermin": 0.01,    "costmin": 0.5},
]

# --- Scoring ---
MIN_RATIO = 5 / 7          # F3: required = max(2, round(MIN_RATIO * achievable))
STRICT_SEVEN = False       # True -> fixed 5-of-7 regardless of achievable
DOWN_WEEKS = 3             # F1: consecutive lower closes required before an up close
PIVOT_MIN_DEPTH = 0.015    # F2: divergence pivot prominence (1.5%)

# --- Freshness / regime ---
STALE_SECS = 180           # F5: tick_age beyond this -> STALE, alerts suppressed
DANGER_DRSI = 30           # §8: danger tag + tier boundary alignment

# --- Alerting ---
# OPERATOR OVERRIDE: F10 cooldown OFF ("no blockers"). 0 makes should_alert()
# always true (now-last >= 0), so a confirmed BUY re-alerts AND re-places a live
# order on every daily/weekly close while it stays BUY — full pyramid/stacking on
# the same symbol (there is no separate dedupe; rails are also off). Set >0 to
# re-arm the per-symbol wait (e.g. 24 = the old once-a-day guard).
REALERT_HOURS = 0          # F10: per-symbol cooldown before re-alert (0 = disabled)
# COUPLING (audit F2): flipping this >0 re-arms the cooldown, but two strands must
# be fixed IN THE SAME change or it's porous exactly when it matters:
#  1. TOCTOU — the check (last_alert_ts) runs on the writer while the insert
#     (alerter.fire) runs in the offloaded dispatch thread, so two near-simultaneous
#     closes both pass. Make check+insert atomic on the writer.
#  2. The alerts table IS the cooldown ledger, but _dispatch now places the order
#     BEFORE alerter.fire and isolates the alert — so a failed alert records NO fire
#     for an order that DID happen, blinding the cooldown to it. When re-enabling,
#     record the fire on the ORDER path (not the decoration path), so the ledger
#     reflects orders placed, not alerts that happened to succeed.
PROVISIONAL_ALERTS = False # invariant 7: provisional is display-only unless True

# --- Conviction multipliers (F8): score relative to required threshold ---
CONVICTION = {0: 1.0, 1: 2.0, 2: 3.0}  # +1 -> 2x, +2 and above -> 3x (STARTER at 0)

# --- Named horizontal price levels (F7), display-only, operator-edited ---
LEVELS = {
    "BTC/USD": [("62.8k", 62858), ("57.6k", 57585)],
}

# --- UI cadence ---
SIMPLE_SECS = 60           # plaintext frame period in --simple mode
RENDER_HZ = 2              # rich Live render cap
FLASH_SECS = 0.6           # tick-direction tint window; >= one render period at
                           # RENDER_HZ=2 so the flash is actually visible (spec's
                           # ~300ms would fall between frames half the time)

# --- Candle-close clock fallback (SPEC §5b) ---
# The WS ohlc feed sends NOTHING across an interval border until the next trade.
# The clock watchdog detects a forming bar past its deadline (+grace), REST-
# confirms the closed bar, flips it, and triggers the confirmed recompute.
CLOSE_GRACE_SECS = 5
CLOSE_POLL_SECS = 15

# --- REST throttle (Appendix B) ---
MIN_CALL_GAP = 0.6
FETCH_RETRIES = 2

# --- Telegram: env only, never in files, never committed (§10/§11) ---
TG_TOKEN = os.environ.get("ORACLE_TG_TOKEN")
TG_CHAT = os.environ.get("ORACLE_TG_CHAT")

# ═══════════════════════════════════════════════════════════════════════════
# EXECUTION — live Kraken spot-margin (operator override, docs/RULINGS.md).
# Deterministic: signal fires -> size -> open leveraged long -> rest stop -> log.
# NO learning brain. Off by default; nothing can fire until EXEC_MODE flips.
# ═══════════════════════════════════════════════════════════════════════════
# EXEC_MODE is fail-CLOSED: only the EXACT canonical strings arm anything. Any other
# value — a case slip ('LIVE'), a trailing space ('paper ', trivial in .env/systemd),
# or a safe-sounding typo ('test'/'sim'/'dry') — resolves to 'off' with a loud error.
# We deliberately do NOT lower()/strip()-coerce: coercing 'LIVE'->'live' would arm real
# money on a typo. Every downstream gate is a BLOCKLIST (mode=='off' returns early, else
# the code falls THROUGH to the live AddOrder path), and poll_fills/verify_open_stops are
# gated on exact 'live' — so an unrecognized mode that slipped past would place real
# leveraged orders that never get a protective stop or fill-reconcile. Catch it here.
_VALID_EXEC_MODES = ("off", "paper", "validate", "live")


def _normalize_exec_mode(raw):
    if raw in _VALID_EXEC_MODES:
        return raw
    _log.error("DEEPFIELD_EXEC_MODE=%r is not one of %s — refusing to arm; running OFF.",
               raw, _VALID_EXEC_MODES)
    return "off"


EXEC_MODE = _normalize_exec_mode(os.environ.get("DEEPFIELD_EXEC_MODE", "off"))   # off | paper | validate | live

# Web console — served in-process by the live bot (a daemon thread) so the one
# desktop launch brings up TUI + web together. Read-only; set DEEPFIELD_WEB=0 to
# disable. The desktop launcher opens the browser to this port.
WEB_ENABLED = os.environ.get("DEEPFIELD_WEB", "1") != "0"
WEB_PORT = int(os.environ.get("DEEPFIELD_WEB_PORT", "8787"))

# Sizing. "min" (default, for now): buy the MINIMUM order per pair — positions so
# small nothing meaningful is ever at risk, so liquidation is a non-issue. "risk":
# 2% of equity off the stop (kept for later; revisit stop-vs-liquidation first).
EXEC_SIZE_MODE = os.environ.get("DEEPFIELD_EXEC_SIZE", "min")   # min | risk
RISK_PCT = 0.02
PAPER_PORTFOLIO_USD = 1000.0        # equity used for sizing math in paper/off

# Per-order sanity ceiling (Finding 8): refuse any single order whose NOTIONAL
# (volume x entry — the leveraged position size, i.e. the blast radius) exceeds this.
# NOT a rail: it never halts the bot and never shrinks a valid min-size order (min
# notionals run ~$3-8). It converts "a corrupt `pairs` row or a flipped EXEC_SIZE_MODE
# silently changes the blast radius" into a refused order + a loud log — making
# min-sizing a CHECKED bound, not just a config knob. 0 = disabled; raise it if you
# deliberately move to larger risk-mode sizing.
EXEC_MAX_ORDER_NOTIONAL_USD = 50.0

# Stop: weekly support (bottom-thesis invalidation), clamped to a sane band so a
# razor-thin stop can't blow up position size and a far one can't dust it.
STOP_MODE = "support"               # support | pct
STOP_PCT = 0.10                     # used when STOP_MODE="pct"
STOP_MIN_PCT = 0.05
STOP_MAX_PCT = 0.15
PROTECTIVE_STOP = True              # rest a real stop on the exchange (kill-safe)

ENTRY_ORDERTYPE = "limit"           # post-only maker ONLY (no market entries). A resting
                                    # limit is recorded status='pending' and promoted to
                                    # 'open' only when the fill monitor confirms it filled.
POST_ONLY_SLIP_PCT = 0.001          # bid this far BELOW last so the post-only maker can't
                                    # cross the ask (a crossing post-only is rejected -> silent
                                    # no-fill). 10bps ~= a patient bottom bid; negligible cost.
ENTRY_TTL_SECS = 86400              # cancel a still-unfilled post-only entry bid after this
                                    # long (default 1 day) so stale bids don't pile up against
                                    # Kraken's open-order cap and crowd out protective stops
                                    # (Finding 5). Fills are unaffected (a filled bid is 'open',
                                    # not 'pending'), so stacking still works. 0 = never expire.
# Continuous laddering: when a resting entry FILLS, immediately drop the next rung one
# LADDER_STEP_PCT below the fill (post-only, min-fill, SAME support stop) so accumulation
# continues down toward the stop without waiting for a candle close or a restart. Bounded
# by a NATURAL FLOOR — a rung that would land at/under the stop is not placed — so a full
# descent is ~ (entry-stop)/step rungs (~8 at 1% over an ~8% stop), never a runaway. One
# resting rung per symbol at a time; a gap-down that puts the rung above market is rejected
# by post-only (ladder pauses, safe) until the next fill/close. LIVE mode only.
LADDER_CONTINUOUS = True
LADDER_STEP_PCT = 0.01              # next rung this far below the fill (1% ~= 8 rungs to an 8% stop)
LADDER_STOP_BUFFER = 0.0            # extra margin ABOVE the stop below which no rung is placed
MARGIN_CAP_PCT = 0.90               # a single position may post at most this frac of free margin

# Rung/entry size multiplier (operator 2026-07-13 "bigger rungs, stack as much as
# possible"): every min-mode order — confirmed-BUY entries, ladder rungs, seeds —
# is sized at SIZE_MULT x the min fill; conviction (2x/3x) stacks ON TOP, so a 3x-
# conviction rung at SIZE_MULT=3 is 9x min (~$30-45 notional — still under the
# conviction-scaled EXEC_MAX_ORDER_NOTIONAL ceiling). Fail-safe: an unparseable
# env override runs at 1x (min), never at a surprise size.
try:
    SIZE_MULT = max(1.0, float(os.environ.get("DEEPFIELD_SIZE_MULT", "3")))
except ValueError:
    _log.error("DEEPFIELD_SIZE_MULT=%r is not a number — running at 1x (min size)",
               os.environ.get("DEEPFIELD_SIZE_MULT"))
    SIZE_MULT = 1.0

# Seeded chains (operator 2026-07-13 "open the ten 10:1 pairs"): every pair below
# keeps a ladder chain WORKING at all times — a pair with no open rows and no
# resting bid gets a post-only starter bid just below live, NOT gated on a
# confirmed BUY (the backtest showed the signal is beta; the ladder is the
# strategy). The 5x/2x pairs (AAVE/UNI/DOT/BCH/ALGO) are deliberately excluded —
# they eat 2-5x the margin per dollar of notional. Regime gate + HALT + all rung
# guards still apply. Empty tuple disables seeding.
SEED_PAIRS = ("BTC/USD", "ETH/USD", "XRP/USD", "SOL/USD", "SUI/USD",
              "DOGE/USD", "LTC/USD", "LINK/USD", "ADA/USD", "AVAX/USD")

# Equity take-profit (operator 2026-07-13 "t/p out at +20%, then go again"): when
# live equity >= tp_baseline * (1 + TP_PCT), flatten the WHOLE book — cancel every
# resting bid and protective stop, market-close each pair's live open volume —
# then reset the baseline to post-flatten equity and let the seeder restack: a
# compounding stack->harvest->restack cycle. The closes are EVENT-TRIGGERED
# market orders sized to Kraken's OpenPositions volume at that instant — never a
# resting sell, so the no-resting-sell / net-short rule below stands intact.
TP_ENABLED = True
TP_PCT = 0.20

# Runtime exchange-truth sweep (audit 2026-07-13 #1): re-run the startup ledger↔Kraken
# reconcile (verify_open_stops) every this-many seconds from the poll cycle, so an
# intraday stop-fire, force-liquidation, or manual close is noticed within minutes —
# not at the next restart. Costs ~4 private API calls per pass at the current book.
# 0 disables (restores boot-only reconcile). NOT a rail: it only trues the ledger up
# to the exchange and cancels provably-orphaned stops; it never blocks an entry.
RUNTIME_RECON_SECS = 900

# Margin-level watch (audit 2026-07-13 #2). Kraken margin-calls at ml<=80% and force-
# liquidates from ml<=40% — bypassing every stop, invisibly to the ledger. This is
# protection against the EXCHANGE seizing the book, not a self-brake:
#  - below MARGIN_LEVEL_ALERT_PCT: fire a (throttled) safety alert. Display + noise only.
#  - below MARGIN_LEVEL_STACK_FLOOR_PCT: pause SEEDS and LADDER RUNGS only — confirmed-BUY
#    signal entries are NEVER gated by this (operator no-blockers stance). FAILS OPEN:
#    a stale/unknown margin level never pauses anything.
# 0 disables either threshold.
MARGIN_LEVEL_ALERT_PCT = 150
MARGIN_LEVEL_STACK_FLOOR_PCT = 120

# Safety-alert channel (audit 2026-07-13 #3): sound + notify-send (+ Telegram iff the
# env vars are set) for money-path safety events — UNPROTECTED positions, reconcile
# mismatches, margin-level danger, T/P cycle events. Throttled per event-kind so a
# retry loop can't turn the speaker into a siren. 0 disables throttling (every event).
SAFETY_ALERT_THROTTLE_SECS = 1800

# Rollover-fee accounting (audit 2026-07-13 #2): Kraken charges 0.01-0.05% of notional
# per 4h to hold a margin position. Poll the Ledgers API for rollover/margin entries
# at this cadence and accumulate the paid fees into meta (fees_total / fees_day) so
# the drag is VISIBLE next to realized P&L instead of silently mimicking market losses.
# Display/accounting only — never gates an order. 0 disables the poll.
ROLLOVER_POLL_SECS = 3600

# FORK A regime gate: accumulate in weakness, not strength. When True, new confirmed
# entries AND ladder rungs are placed ONLY when the BTC regime is not confirmed BULL
# ("stop adding once BULL" — buy the fall/turn, not the strength). FAILS OPEN: an
# unknown/missing/other regime (BEAR/RECOVERY/NEUTRAL/UNKNOWN) still accumulates, so a
# stale or unavailable regime can never silently halt entries (operator no-blockers
# stance). Only the unambiguous BULL state pauses accumulation. Set False to disable.
ACCUMULATE_ONLY_IN_BEAR = True
NO_ACCUMULATE_REGIMES = ("BULL",)   # regimes that pause new entries/rungs when the gate is on
# NOTE: the strategy is LONG ONLY — a resting sell can net short (Kraken spot-margin has
# no reduce_only). The only sells are protective STOPS (sized to close a long) and the
# TP_ENABLED equity flatten above (event-triggered market closes sized to live open
# volume at that instant, operator-ordered 2026-07-13). Do NOT add resting sells.

# Risk rails (deterministic hard limits, from GoldenEye — NOT learners).
# OPERATOR OVERRIDE: automatic circuit breakers OFF ("no circuit breakers, no
# fear"). RAILS_ENABLED=False makes rails_ok skip the drawdown kill-switch, the
# daily/weekly loss caps, the max-positions gate, and the equity-unknown block —
# the bot never stops ITSELF. The manual HALT file (below) stays as the operator's
# hand-on-switch, and the per-position protective stop is the strategy's own exit,
# neither of which is a "circuit breaker". Flip True to re-arm the auto-brakes.
RAILS_ENABLED = False
MAX_OPEN_POSITIONS = 15
DAILY_LOSS_LIMIT_USD = 15.0         # halt new entries after this realized daily loss
WEEKLY_LOSS_LIMIT_USD = 35.0
KILL_SWITCH_DD_PCT = 0.20           # halt at 20% drawdown from peak equity (manual reset)
HALT_FILE = os.path.join(PROJECT_ROOT, "deepfield.HALT_ENTRIES")  # touch to halt / rm to resume

# Per-pair leverage — a FIXED hardcoded value Kraken must accept verbatim (it must
# be present in the pair's leverage_buy array). Sent exactly as-is on every order,
# hydra-style. Verified 2026-07-04 == max Kraken leverage_buy per pair. HARDCODED TO
# THE PER-PAIR MAX ON PURPOSE — do NOT lower. (The fork-A 2x de-lever was a mistake and
# was reverted 2026-07-11 at operator direction.)
PER_PAIR_LEVERAGE = {
    "BTC/USD": 10, "ETH/USD": 10, "XRP/USD": 10, "SOL/USD": 10, "DOGE/USD": 10,
    "ADA/USD": 10, "LINK/USD": 10, "SUI/USD": 10, "LTC/USD": 10, "AVAX/USD": 10,
    "AAVE/USD": 5, "UNI/USD": 5, "DOT/USD": 5, "BCH/USD": 5, "ALGO/USD": 2,
    # 5x expansion (each is Kraken's per-pair max — the hard ceiling, never lower)
    "CRV/USD": 5, "HBAR/USD": 5, "PEPE/USD": 5,
    "SHIB/USD": 5, "TRX/USD": 5, "ZEC/USD": 5,
}
# Leveraged orders MUST use the :BTNL margin-book name (Non-ECP rejects spot name).
MARGIN_PAIR = {
    "BTC/USD": "XBTUSD:BTNL", "ETH/USD": "ETHUSD:BTNL", "XRP/USD": "XRPUSD:BTNL",
    "SOL/USD": "SOLUSD:BTNL", "DOGE/USD": "XDGUSD:BTNL", "ADA/USD": "ADAUSD:BTNL",
    "LINK/USD": "LINKUSD:BTNL", "SUI/USD": "SUIUSD:BTNL", "LTC/USD": "LTCUSD:BTNL",
    "AVAX/USD": "AVAXUSD:BTNL", "AAVE/USD": "AAVEUSD:BTNL", "UNI/USD": "UNIUSD:BTNL",
    "DOT/USD": "DOTUSD:BTNL", "BCH/USD": "BCHUSD:BTNL", "ALGO/USD": "ALGOUSD:BTNL",
    # 5x expansion — altname:BTNL, all validated by --exec-probe (real Kraken order-check)
    "CRV/USD": "CRVUSD:BTNL", "HBAR/USD": "HBARUSD:BTNL", "PEPE/USD": "PEPEUSD:BTNL",
    "SHIB/USD": "SHIBUSD:BTNL",
    "TRX/USD": "TRXUSD:BTNL", "ZEC/USD": "ZECUSD:BTNL",
}
# :BTNL margin-book PRICE precision (differs from spot — too many decimals rejects).
MARGIN_TICK_DECIMALS = {
    "BTC/USD": 1, "ETH/USD": 2, "XRP/USD": 5, "SOL/USD": 2, "DOGE/USD": 7,
    "ADA/USD": 6, "LINK/USD": 5, "SUI/USD": 4, "LTC/USD": 2, "AVAX/USD": 2,
    "AAVE/USD": 2, "UNI/USD": 3, "DOT/USD": 4, "BCH/USD": 2, "ALGO/USD": 5,
    # 5x expansion — :BTNL margin-book precision, confirmed by --exec-probe. CRV (4)
    # and SHIB (8) are LESS than their spot pair_decimals (5 and 9) — the margin book
    # is coarser, exactly the "differs from spot" case; probe rejected the spot value.
    "CRV/USD": 4, "HBAR/USD": 5, "PEPE/USD": 9,
    "SHIB/USD": 8, "TRX/USD": 6, "ZEC/USD": 2,
}
```

## `deepfield/broker.py` (351 lines)

```python
"""Kraken private API — auth/nonce/signing ported from hydra `_kraken_private`.

Operator override (see docs/RULINGS.md): DEEPFIELD now places live margin orders.
This module is the signed-request layer only; order construction lives in
executor.py. Field-proven pattern from the operator's hydra.py.

Keys: two lines (key, then secret) in ~/.deepfield_keys, falling back to
~/.hydra_keys. **Use a DEDICATED Kraken API key for DEEPFIELD** — Kraken's nonce
is per-API-key, so DEEPFIELD and hydra sharing one key while both run would
collide nonces ("Invalid nonce"/"Invalid key"). Separate keys = separate nonce
sequences = no war.

Every private call is RAW-logged (nonce masked; key/sign live only in headers,
never logged) to logs/deepfield_orders_raw.log — the audit trail hydra taught.
"""
import os
import time
import json
import base64
import hashlib
import hmac
import logging
import threading
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("deepfield.broker")
_raw = logging.getLogger("deepfield.broker.raw")

BASE_URL = "https://api.kraken.com"
KEYFILES = [os.path.expanduser("~/.deepfield_keys"), os.path.expanduser("~/.hydra_keys")]
NONCE_FILE = os.path.expanduser("~/.deepfield_nonce")

_LAST_NONCE = 0
# _next_nonce is a read-modify-write on _LAST_NONCE + a write to NONCE_FILE, and
# private() runs concurrently from several threads (per-alert dispatch, poll_fills,
# equity refresh). Without this lock two threads can mint the SAME microsecond nonce
# -> one call rejected 'EAPI:Invalid nonce', and the file write can tear (Finding 7).
_NONCE_LOCK = threading.Lock()
# _NONCE_LOCK alone isn't enough: it serializes nonce MINTING, but the lock is freed
# before the HTTP send, so two threads can mint n and n+1 then have their requests
# ARRIVE at Kraken out of order (n+1 first) — Kraken rejects the later, lower nonce
# ('EAPI:Invalid nonce'). Kraken's nonce is strictly-increasing per key, so private
# calls simply cannot be concurrent: _SEND_LOCK makes mint→send atomic, so requests
# reach Kraken in the same monotonic order their nonces were minted.
_SEND_LOCK = threading.Lock()
_KEY = None
_SECRET = None
_KEY_SRC = None


def load_keys():
    """(key, secret, source_path) or (None, None, None). Cached after first hit."""
    global _KEY, _SECRET, _KEY_SRC
    if _KEY is not None:
        return _KEY, _SECRET, _KEY_SRC
    for path in KEYFILES:
        try:
            with open(path) as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if len(lines) >= 2:
                _KEY, _SECRET, _KEY_SRC = lines[0], lines[1], path
                return _KEY, _SECRET, _KEY_SRC
        except Exception:
            continue
    return None, None, None


def keys_present():
    k, s, _ = load_keys()
    return bool(k and s)


def _next_nonce():
    """Strictly-increasing, restart-safe (hydra pattern): seed from
    max(clock, persisted+1), persist every call."""
    global _LAST_NONCE
    with _NONCE_LOCK:                       # serialize the whole RMW + file write
        n = int(time.time() * 1_000_000)
        if _LAST_NONCE == 0:
            try:
                p = int((open(NONCE_FILE).read().strip() or "0"))
                # Persisted high-water beats a stale/backward wall clock — NO upper cap.
                # A VM-snapshot restore / NTP step-back makes the clock < the last nonce
                # Kraken saw; the old 1h window skipped this and wedged ALL private calls
                # (Invalid nonce) while clobbering the good high-water. Always trust p.
                if p >= n:
                    n = p + 1
            except Exception:
                pass
        if n <= _LAST_NONCE:
            n = _LAST_NONCE + 1
        _LAST_NONCE = n
        try:
            with open(NONCE_FILE, "w") as f:
                f.write(str(n))
        except Exception:
            pass
        return str(n)


def sign(path, postdata, nonce, secret_b64):
    """Kraken API-Sign: base64(HMAC-SHA512(secret, path + SHA256(nonce+postdata)))."""
    secret = base64.b64decode(secret_b64)
    sha256 = hashlib.sha256((nonce + postdata).encode()).digest()
    return base64.b64encode(hmac.new(secret, path.encode() + sha256, hashlib.sha512).digest()).decode()


def private(endpoint, params=None, idempotent=True, meta=None):
    """Signed POST to a Kraken private endpoint. Returns 'result' dict or None.
    Retries nonce/rate ERRORS (from a received response — the request did NOT
    execute) with a fresh higher nonce. idempotent=False (AddOrder/CancelOrder):
    a NETWORK exception is NOT retried, because the order may already have landed
    and a blind resend would DUPLICATE it (a duplicate stop can open a short).

    meta (audit 2026-07-13): pass a dict to learn HOW a None happened —
    meta['definite']=True means Kraken RESPONDED (an error reject: the order did
    not execute); False means the network failed and the request MAY have landed.
    Callers that record 'rejected' must only do so on definite=True; an ambiguous
    None needs the userref recovery path, not a terminal status."""
    key, secret, _ = load_keys()
    if meta is not None:
        meta["definite"] = False
    if not key or not secret:
        log.error("no Kraken API keys (looked in %s) — cannot send %s", KEYFILES, endpoint)
        if meta is not None:
            meta["definite"] = True      # nothing was sent — definitely not on the book
        return None
    base = dict(params or {})
    url = BASE_URL + endpoint
    for attempt in range(5):
        wait = 0.0                              # backoff before the next attempt; 0 = give up
        # Hold _SEND_LOCK across mint→send so Kraken sees monotonic nonces (a request
        # can never overtake an earlier one with a lower nonce). Backoff sleeps happen
        # AFTER the lock is released, so a retry never stalls other callers.
        with _SEND_LOCK:
            p = dict(base)
            p["nonce"] = _next_nonce()
            postdata = urllib.parse.urlencode(p)
            sig = sign(endpoint, postdata, p["nonce"], secret)
            headers = {"API-Key": key, "API-Sign": sig,
                       "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "DEEPFIELD/1"}
            try:
                _raw.info("REQ %s %s", endpoint, postdata.replace(p["nonce"], "<nonce>"))
                req = urllib.request.Request(url, data=postdata.encode(), headers=headers)
                with urllib.request.urlopen(req, timeout=15) as r:
                    raw = r.read()
                _raw.info("RESP %s %s", endpoint, raw.decode("utf-8", "replace"))
                data = json.loads(raw)
                err = data.get("error")
                if err:
                    es = str(err)
                    if ("Nonce" in es or "nonce" in es or "Invalid key" in es) and attempt < 4:
                        wait = 0.4
                    elif "Rate limit" in es and attempt < 4:
                        wait = 5.0
                    else:
                        log.error("private API error %s: %s", endpoint, es)
                        if meta is not None:
                            meta["definite"] = True   # Kraken responded: a real reject
                        return None
                else:
                    if meta is not None:
                        meta["definite"] = True
                    return data.get("result")
            except Exception as e:
                log.warning("private API attempt %d failed: %s", attempt + 1, e)
                if not idempotent:
                    log.error("%s not retried after network error — may or may not have "
                              "landed; caller must NOT blind-resend", endpoint)
                    return None
                wait = 2.0
        if wait:                                # lock released — back off, then retry
            time.sleep(wait)
            continue
        return None
    return None


def equity(balance):
    """Account equity in USD from a TradeBalance result: 'e' (balance + unrealized
    net PnL), falling back to 'eb'/'tb'; first >0 wins, else None. ONE definition
    so the dashboard, rails, peak, and the order path can never disagree."""
    if not balance:
        return None
    for k in ("e", "eb", "tb"):
        try:
            v = float(balance.get(k))
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def trade_balance():
    """Live account equity in USD, or None."""
    return equity(private("/0/private/TradeBalance", {"asset": "ZUSD"}))


def open_positions():
    """Open margin positions dict, {} if none, or None on API FAILURE (callers must
    distinguish 'no positions' from 'could not check' — treating a failed check as
    'no positions' would abandon/mis-handle real open longs)."""
    return private("/0/private/OpenPositions")


def open_orders():
    """All currently-resting orders as {txid: order_info}, {} if none, or None on API
    FAILURE. Kraken returns {'open': {txid: {...}}}; we hand back the inner map so a
    caller can find a stop that IS resting on the book but whose txid the ledger lost
    (a persist-race orphan) BEFORE blindly re-placing a duplicate. None (not {}) on
    failure so 'could not check' is never read as 'nothing resting'."""
    r = private("/0/private/OpenOrders")
    if r is None:
        return None
    return r.get("open") or {}


def cancel_order(txid):
    """Cancel an order by txid. Non-idempotent transport (no blind resend)."""
    if not txid:
        return None
    return private("/0/private/CancelOrder", {"txid": txid}, idempotent=False)


def cancel_order_batch(txids):
    """Cancel MANY orders by txid via CancelOrderBatch (50/call), replacing the
    account-wide CancelAll the T/P flatten used to fire (audit 2026-07-13 M4:
    CancelAll also swept manually-placed / other-system orders, and stripped the
    stop off any Kraken position the ledger had no row for — permanently naked).
    Targeted: only OUR txids are touched. Best-effort like CancelAll was — the
    flatten reconciles from definite post-sweep OpenOrders state either way.
    Returns the count Kraken reports canceled, or None if every chunk failed."""
    ids = [t for t in (txids or []) if t]
    if not ids:
        return 0
    total, any_ok = 0, False
    for i in range(0, len(ids), 50):
        # Kraken's form-encoded API wants the `orders` array as indexed keys
        # (orders[0]=A&orders[1]=B). A JSON-string blob OR repeated `orders=` keys
        # are both rejected 'EGeneral:Invalid arguments:orders' (the json.dumps form
        # never worked live — the T/P flatten could never sweep, looping forever).
        chunk = ids[i:i + 50]
        params = {f"orders[{j}]": t for j, t in enumerate(chunk)}
        r = private("/0/private/CancelOrderBatch", params, idempotent=False)
        if r is not None:
            any_ok = True
            try:
                total += int(r.get("count", 0) or 0)
            except (TypeError, ValueError):
                pass
    return total if any_ok else None


def find_order_by_userref(userref):
    """Locate an order by our client userref — the recovery path for an AddOrder
    whose transport failed AMBIGUOUSLY (audit 2026-07-13 C3: without this, an
    order that actually landed becomes an untracked position with no stop).
    Checks resting orders first, then recently-closed ones. Returns
    (txid, order_dict) or (None, None) when definitely absent, or (None, 'unknown')
    when the API could not answer (caller must retry, not conclude)."""
    if not userref:
        return None, None
    oo = private("/0/private/OpenOrders", {"userref": str(userref)})
    if oo is None:
        return None, "unknown"
    for txid, od in (oo.get("open") or {}).items():
        return txid, od
    co = private("/0/private/ClosedOrders", {"userref": str(userref)})
    if co is None:
        return None, "unknown"
    for txid, od in (co.get("closed") or {}).items():
        return txid, od
    return None, None


def rollover_fees_since(start_ts):
    """Sum of margin rollover fees paid since `start_ts` (unix), from the Ledgers
    API (entry type 'rollover'; the charge is in the 'fee' field, asset ZUSD).
    Paginates via ofs (50/page, capped at 20 pages — a month of 4h rollovers on
    ~20 pairs). Returns (total_fee_usd, entry_count, newest_entry_ts) or None on
    API failure. Display/accounting only — never gates an order."""
    total, count, newest = 0.0, 0, float(start_ts or 0)
    ofs = 0
    for _ in range(20):
        r = private("/0/private/Ledgers",
                    {"type": "rollover", "start": str(start_ts or 0), "ofs": str(ofs)})
        if r is None:
            return None
        entries = r.get("ledger") or {}
        if not entries:
            break
        for e in entries.values():
            try:
                total += abs(float(e.get("fee", 0) or 0))
                newest = max(newest, float(e.get("time", 0) or 0))
                count += 1
            except (TypeError, ValueError):
                continue
        got = len(entries)
        ofs += got
        if got < 50:
            break
    return total, count, newest


def trade_balance_full():
    """Full TradeBalance result dict (e=equity, m=margin used, mf=free margin,
    ml=margin level) or None."""
    return private("/0/private/TradeBalance", {"asset": "ZUSD"})


def query_orders(txids):
    """Batch order-info lookup: {txid: info_dict} for many txids in as few calls as
    possible (Kraken QueryOrders takes up to 50 comma-separated txids per request).
    A 60-position startup reconcile otherwise fires 60+ QueryOrders back-to-back and
    trips the private-API rate limit on every restart; this collapses it to ~2 calls.
    A txid ABSENT from the returned map means its status is UNKNOWN — callers must
    treat a missing key exactly like query_order's None (never 'definitely gone',
    which could trigger a blind stop re-place -> duplicate stop -> short). On API
    failure the chunk contributes nothing, so all its txids read as unknown."""
    ids = [t for t in (txids or []) if t]
    out = {}
    for i in range(0, len(ids), 50):                # Kraken cap: 50 txids per call
        r = private("/0/private/QueryOrders", {"txid": ",".join(ids[i:i + 50])})
        if r:
            out.update(r)
    return out


def query_order(txid):
    """Order info dict for a txid (has 'status': open|closed|canceled|...) or None.
    Single-txid case of query_orders (ONE definition, so both paths agree)."""
    if not txid:
        return None
    return query_orders([txid]).get(txid)


def setup_raw_log(log_dir):
    """Route the RAW order audit trail to its own file (append), 5MB x 3."""
    from logging.handlers import RotatingFileHandler
    os.makedirs(log_dir, exist_ok=True)
    h = RotatingFileHandler(os.path.join(log_dir, "deepfield_orders_raw.log"),
                            maxBytes=5 * 1024 * 1024, backupCount=3)
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _raw.handlers = [h]
    _raw.setLevel(logging.INFO)
    _raw.propagate = False
```

## `deepfield/ingest.py` (555 lines)

```python
"""Single writer: consumes the event queue, persists to DB, updates published
state, triggers confirmed/provisional recompute, and gates the alert chain via
F10 cooldown. SPEC §5, invariants 1-3, F5/F10/F13.

The stream is transport, not truth (invariant 3): both confirmed and
provisional recompute re-read the closed/forming series from the DB rather
than accumulating a series in memory from events.

Also owns the §5(b) clock-close fallback: the WS ohlc feed sends NOTHING across
an interval border until the next trade, so a low-volume close can pass silently.
The clock watchdog finds forming bars past deadline (+grace), REST-confirms the
closed bar, flips it, and runs the same confirmed-recompute path a WS close would.
"""
import time
import asyncio
import logging

from . import store
from . import engine
from . import events
from . import alerter
from . import config
from .profiles import FULL
from .state import TrancheInfo
from .config import (REALERT_HOURS, PROVISIONAL_ALERTS, STALE_SECS, FLASH_SECS,
                     CLOSE_GRACE_SECS, CLOSE_POLL_SECS)

log = logging.getLogger("deepfield.ingest")

BTC_SYMBOL = "BTC/USD"

# §5(b) REST-confirm deferral ceiling: how long a clock-detected close may wait
# for a successful REST confirm before we flip+fire on the partial WS data
# anyway. The watchdog retries every CLOSE_POLL_SECS (~15s), so a transient
# outage just delays the close a few polls; a PERMANENT outage must not silence
# closes forever (operator no-blockers doctrine) — after this many seconds past
# the first failed confirm we proceed loudly on clock authority.
REST_CONFIRM_DEFER_SECS = 1800


def _elapsed_fraction(interval_begin, interval_min, now=None):
    now = time.time() if now is None else now
    span = interval_min * 60
    return max(0.0, min(1.0, (now - interval_begin) / span))


class Ingest:
    def __init__(self, conn, appstate, profile=FULL):
        self.conn = conn
        self.state = appstate
        self.profile = profile
        self._pair_info_cache = {}  # symbol -> dict|None; ordermin/costmin/lot_decimals
                                     # are static enough (AssetPairs refresh is daily,
                                     # §10) that per-tick recompute must not hit the DB.
        # Execution is OFF unless EXEC_MODE flips (docs/RULINGS override). When
        # off, self.executor is None and the confirmed-BUY path is signal-only.
        self.executor = None
        self._bg_tasks = set()   # strong refs so fire-and-forget dispatch isn't GC'd
        self._armed_buys = set()  # symbols already-BUY at startup that have fired their
                                  # one-shot boot arm this session (see handle_tick)
        self._last_fired = {}     # symbol -> last close INSTANT (interval_begin+interval*60)
                                  # that fired a confirmed alert/order. Keyed on the close
                                  # instant, NOT (symbol, interval): the WS feed AND the
                                  # clock-close watchdog emit CandleClosed for the same bar,
                                  # AND the daily+weekly bars close at the SAME instant every
                                  # Thu 00:00 UTC — both must collapse to ONE fire. (flip_closed's
                                  # rowcount can't be trusted: upsert_candle already set closed=1
                                  # on the clock-close/late-update path.) Fire once per NEW instant.
        self._rest_defer = {}     # (symbol, interval, forming_ts) -> unix ts of the FIRST
                                  # failed REST confirm for that bar. A clock-close whose
                                  # REST confirm fails is DEFERRED (no flip, no fire) so the
                                  # watchdog's next find_overdue pass retries it — until
                                  # REST_CONFIRM_DEFER_SECS, when we flip+fire loudly anyway.
                                  # Pruned on every successful confirm/close so it can't grow.
        self._boot_buys = None    # set of symbols BUY at BOOT (first startup_sweep). The
                                  # one-shot arm may fire ONLY these — otherwise a reconciler
                                  # resweep / 'f'-key that flips a symbol to BUY mid-session
                                  # would fire a live order from a path documented alert-silent
                                  # (Finding 3). None until the boot sweep populates it.
        if config.EXEC_MODE != "off":
            from . import executor as executor_mod
            self.executor = executor_mod.Executor(conn)
            log.warning("EXECUTION ENABLED — mode=%s (live leveraged orders on confirmed BUYs)",
                        config.EXEC_MODE)

    def _journal(self, kind, symbol, text):
        """Isolated journal emit (v6 JOURNAL view) — display-truth narration that
        must NEVER delay or drop the alert/order path. Never raises out."""
        try:
            store.journal(self.conn, kind, symbol, text)
        except Exception:
            log.exception("journal emit failed (%s %s) — path unaffected", kind, symbol)

    # ── event handlers ──────────────────────────────────────────────────────

    def handle_tick(self, ev: events.Tick):
        ps = self.state.pair(ev.symbol)
        if ps.last_tick is not None and ev.last != ps.last_tick.last:
            ps.flash_color = "green" if ev.last > ps.last_tick.last else "red"
            ps.flash_until = time.monotonic() + FLASH_SECS
        ps.last_tick = ev
        ps.last_tick_ts = ev.ts
        # Keep the champion card's tranche priced off the live tick, not stale
        # from the last close/startup-sweep — found via the M6 export proof,
        # where "Live entry" and "Tranche" showed two different prices.
        if ps.confirmed is not None:
            self._compute_tranche(ev.symbol, ps.confirmed)
            self._maybe_arm_startup_buy(ev.symbol, ps)

    def handle_candle_update(self, ev: events.CandleUpdate):
        closed = 1 if time.time() >= ev.interval_begin + ev.interval * 60 else 0
        store.upsert_candle(self.conn, ev.symbol, ev.interval, ev.interval_begin,
                             ev.o, ev.h, ev.l, ev.c, ev.v, closed)
        self.conn.commit()
        # Interval boundaries are shared across all 15 pairs — any pair's
        # forming-bar interval_begin drives the UI countdown region (§8).
        # Monotonic guard: a late-arriving update for the OLD bar (in flight
        # during a border) must never wind the countdown backwards.
        self._advance_countdown(ev.interval, ev.interval_begin)
        self._maybe_recompute_provisional(ev.symbol)

    def handle_candle_closed(self, ev: events.CandleClosed):
        n = store.flip_closed(self.conn, ev.symbol, ev.interval, ev.interval_begin)
        self.conn.commit()
        if n == 0:
            row = self.conn.execute(
                "SELECT closed FROM candles WHERE pair=? AND interval=? AND ts=?",
                (ev.symbol, ev.interval, ev.interval_begin),
            ).fetchone()
            if row is None:
                log.warning("CandleClosed for a row not in DB yet: %s/%s ts=%d (reconciler will gap-heal)",
                            ev.symbol, ev.interval, ev.interval_begin)
            else:
                log.debug("CandleClosed duplicate (already closed): %s/%s ts=%d",
                          ev.symbol, ev.interval, ev.interval_begin)
        # The old provisional card still counts the just-closed bar as forming —
        # honest display is "unknown" until fresh forming data arrives (seconds).
        self.state.pair(ev.symbol).provisional = None
        # Edge-gate the alert/exec on FIRST sight of this bar close. Two distinct
        # duplicate-sources exist: (1) the WS feed AND the clock-close watchdog both emit
        # CandleClosed for the same bar, and (2) the daily (1440) and weekly (10080) bars
        # close at the SAME instant every Thu 00:00 UTC (Kraken's weekly boundary) — one
        # confirmed-BUY verdict that, keyed per-interval, fired TWICE and double-placed a
        # live order (the ADA 2026-07-09 rung). Key the edge on the close INSTANT
        # (interval_begin + interval*60) per symbol so both collapse to ONE fire. (Not
        # flip_closed's rowcount: upsert_candle already flips closed=1 on the clock-close/
        # late-update path, so it would be 0 and wrongly SUPPRESS the fire on quiet pairs.)
        close_at = ev.interval_begin + ev.interval * 60
        fire = close_at > self._last_fired.get(ev.symbol, -1)
        if fire:
            self._last_fired[ev.symbol] = close_at
            # Coincident-border stale-weekly fix (audit 2026-07-13): weekly bars are
            # epoch-anchored, so EVERY weekly border is also a daily border (Thu 00:00
            # UTC). The dedup above gives exactly one of the two coincident events
            # fire=True — but flip_closed (top of this handler) flipped only THIS
            # event's bar, so the fire=True recompute would read a closed-series that
            # still EXCLUDES the other interval's just-completed bar (closed=0): six of
            # seven signals are weekly-driven, and daily-first ordering made the weekly
            # bar a whole week stale at evaluation time. Before the single recompute,
            # flip the OTHER interval's just-completed bar too — symmetric, so whichever
            # event arrives first flips both. flip_closed is idempotent (0 if already
            # closed); a missing row is the reconciler's gap to heal, not ours to block on.
            # This runs only inside the fire=True edge, so the ed74968 double-fire dedup
            # is untouched: the second coincident event still arrives with fire=False.
            for other in config.INTERVALS:
                if other == ev.interval or close_at % (other * 60) != 0:
                    continue
                other_ts = close_at - other * 60
                n_other = store.flip_closed(self.conn, ev.symbol, other, other_ts)
                if n_other:
                    self.conn.commit()
                    log.info("coincident border: flipped %s/%d ts=%d closed alongside %d "
                             "close so the recompute sees BOTH completed bars",
                             ev.symbol, other, other_ts, ev.interval)
                else:
                    log.debug("coincident border: %s/%d ts=%d already closed or not in DB "
                              "yet (gap-heal covers it)", ev.symbol, other, other_ts)
                # Either way the bar is being handled here — drop any REST-confirm
                # deferral tracked for it.
                self._rest_defer.pop((ev.symbol, other, other_ts), None)
        # This bar is closed by whatever path got us here — prune its deferral entry.
        self._rest_defer.pop((ev.symbol, ev.interval, ev.interval_begin), None)
        self._recompute_confirmed(ev.symbol, fire=fire)
        if ev.symbol == BTC_SYMBOL:
            self._recompute_regime()

    def handle_link_up(self, ev: events.LinkUp):
        entry = self.state.links.setdefault(ev.name or "?", {"up": False, "reconnects": 0})
        entry["up"] = True
        entry["reconnects"] = ev.reconnect_count
        self.state.link_up = self.state.link_status() == "UP"
        self.state.reconnect_count = self.state.total_reconnects()

    def handle_link_down(self, ev: events.LinkDown):
        entry = self.state.links.setdefault(ev.name or "?", {"up": False, "reconnects": 0})
        entry["up"] = False
        self.state.link_up = self.state.link_status() == "UP"

    def handle_recon_repair(self, ev: events.ReconRepair):
        self.state.recon_repairs += 1

    async def run(self, queue):
        dispatch = {
            events.Tick: self.handle_tick,
            events.CandleUpdate: self.handle_candle_update,
            events.CandleClosed: self.handle_candle_closed,
            events.LinkUp: self.handle_link_up,
            events.LinkDown: self.handle_link_down,
            events.ReconRepair: self.handle_recon_repair,
        }
        while True:
            ev = await queue.get()
            handler = dispatch.get(type(ev))
            if handler is None:
                continue
            try:
                handler(ev)
            except Exception:
                # Invariant 1/2: the single writer must never die. A poisoned
                # event is logged loudly and skipped; the stream continues.
                log.exception("writer: handler failed for %s — event skipped: %r",
                              type(ev).__name__, ev)

    # ── startup ──────────────────────────────────────────────────────────────

    def startup_sweep(self):
        """Populate confirmed ScoreCards + regime from the DB's closed series
        right after warm-backfill — otherwise the TUI launches blank and stays
        blank until the next real candle close (up to 7 days for weekly).
        Also used by `--once` and after reconciler repairs. No alerts fire here:
        this is not a live transition, just publishing already-true state (F10
        cooldown is keyed off real confirmed-BUY *events*)."""
        buys = set()
        for p in config.PAIRS:
            symbol = p["ws"]
            weekly, daily = store.load_weekly_daily_closed(self.conn, symbol)
            card = engine.evaluate(symbol, weekly, daily, self.profile, provisional=False)
            self.state.pair(symbol).confirmed = card
            self._compute_tranche(symbol, card)
            if card.status == "BUY":
                buys.add(symbol)
        # The FIRST sweep (boot) defines which symbols the one-shot arm may fire. Later
        # sweeps (hourly reconciler / 'f'-key) republish state but must NOT expand it —
        # a symbol they flip to BUY only fires through a real close, never the boot arm.
        if self._boot_buys is None:
            self._boot_buys = buys
            # Seed the per-close fire-dedup from the DB so a restart doesn't re-fire a
            # bar that already closed (and fired) pre-restart: a late WS close for the
            # straddled border would otherwise fire AGAIN on top of the boot arm — two
            # orders from one restart. A genuinely newer bar (close instant > seed) still
            # fires. Seed the same close-INSTANT the edge compares (interval_begin +
            # interval*60), per symbol; the latest daily close instant dominates the weekly.
            for p in config.PAIRS:
                for interval in config.INTERVALS:
                    mt = store.max_closed_ts(self.conn, p["ws"], interval)
                    if mt is not None:
                        self._last_fired[p["ws"]] = max(
                            self._last_fired.get(p["ws"], -1), mt + interval * 60)
        self._recompute_regime()

    # ── §5(b) clock-close fallback ───────────────────────────────────────────

    def find_overdue(self, now=None, grace=CLOSE_GRACE_SECS):
        """Forming bars past their close deadline (+grace) — the silent-border
        case where no trade has arrived to roll the WS feed. Returns
        [(ws_symbol, rest_pair, interval, forming_ts), ...]."""
        now = time.time() if now is None else now
        rest_by_ws = {p["ws"]: p["rest"] for p in config.PAIRS}
        overdue = []
        for p in config.PAIRS:
            ws = p["ws"]
            for interval in config.INTERVALS:
                f = store.get_forming(self.conn, ws, interval)
                if f is None:
                    continue
                if now >= f["ts"] + interval * 60 + grace:
                    overdue.append((ws, rest_by_ws[ws], interval, f["ts"]))
        return overdue

    def apply_rest_confirm(self, ws, interval, forming_ts, rows):
        """REST-confirm a clock-detected close: upsert the authoritative closed
        bar (and the new forming bar if present), then run the exact same
        close path a WS CandleClosed would.

        Confirm-or-defer (audit 2026-07-13): if the REST fetch FAILED (rows is
        None/empty — outage), do NOT flip the bar closed and do NOT fire — that
        would promote partial pre-outage WS data to closed truth and place live
        orders on it. Leave the bar forming; the watchdog's next find_overdue
        pass (~every CLOSE_POLL_SECS) retries. After REST_CONFIRM_DEFER_SECS of
        failed confirms for the same bar, flip+fire anyway with a loud warning
        so a permanent REST outage can't silence closes forever (no-blockers)."""
        now = int(time.time())
        key = (ws, interval, forming_ts)
        found = False
        if rows:
            self._rest_defer.pop(key, None)   # confirm succeeded — clear any deferral
            for r in rows[-4:]:  # only the recent tail is relevant
                ts = int(r[0])
                if ts < forming_ts:
                    continue
                closed = 1 if now >= ts + interval * 60 else 0
                store.upsert_candle(self.conn, ws, interval, ts,
                                     float(r[1]), float(r[2]), float(r[3]),
                                     float(r[4]), float(r[6]), closed)
                if ts == forming_ts:
                    found = True
                if closed == 0:
                    self._advance_countdown(interval, ts)
            self.conn.commit()
        else:
            # REST confirm failed (outage). Defer: no flip, no fire — retry next pass.
            first_seen = self._rest_defer.setdefault(key, now)
            waited = now - first_seen
            if waited < REST_CONFIRM_DEFER_SECS:
                log.warning("clock-close: REST confirm FAILED for %s/%s ts=%d — deferring "
                            "flip/fire (%ds of %ds); watchdog will retry",
                            ws, interval, forming_ts, waited, REST_CONFIRM_DEFER_SECS)
                return
            # Deferral ceiling hit: proceed on clock authority + partial WS data,
            # loudly. Hourly reconciler will true-up the values afterwards.
            self._rest_defer.pop(key, None)
            log.warning("clock-close: REST confirm still failing after %ds for %s/%s ts=%d — "
                        "DEFERRAL CEILING hit; flipping + firing on partial WS data "
                        "(no-blockers: a permanent REST outage must not silence closes)",
                        waited, ws, interval, forming_ts)
        if not found and rows:
            # REST answered but didn't return the bar (shouldn't happen) — flip on
            # clock authority alone; hourly reconciler will true-up the values.
            log.warning("clock-close: REST confirm missing bar %s/%s ts=%d — flipping on clock",
                        ws, interval, forming_ts)
        log.info("CLOCK CLOSE confirmed %s/%s ts=%d (silent border — no trade rolled the feed)",
                 ws, interval, forming_ts)
        self.handle_candle_closed(events.CandleClosed(ws, interval, forming_ts))

    async def clock_close_watchdog(self, fetch_ohlc, poll_secs=CLOSE_POLL_SECS):
        """Poll for overdue forming bars; REST-confirm each (throttled fetch in
        a thread, DB writes back on the loop — single-writer preserved)."""
        while True:
            await asyncio.sleep(poll_secs)
            try:
                overdue = self.find_overdue()
                # Prune deferral entries for bars no longer overdue (e.g. closed by
                # gap-heal, which upserts closed=1 without a CandleClosed event) so
                # self._rest_defer cannot grow without bound.
                live_keys = {(ws, interval, forming_ts)
                             for ws, _rest, interval, forming_ts in overdue}
                for k in list(self._rest_defer):
                    if k not in live_keys:
                        self._rest_defer.pop(k, None)
                for ws, rest, interval, forming_ts in overdue:
                    rows = await asyncio.to_thread(fetch_ohlc, rest, interval)
                    self.apply_rest_confirm(ws, interval, forming_ts, rows)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("clock-close watchdog pass failed (will retry)")

    # ── recompute + alert gating ────────────────────────────────────────────

    def _advance_countdown(self, interval, interval_begin):
        if interval == 1440:
            if interval_begin > (self.state.daily_interval_begin or 0):
                self.state.daily_interval_begin = interval_begin
        elif interval == 10080:
            if interval_begin > (self.state.weekly_interval_begin or 0):
                self.state.weekly_interval_begin = interval_begin

    def _recompute_confirmed(self, symbol, fire=True):
        """Recompute the confirmed card (always, for display). fire=False recomputes
        WITHOUT alerting/ordering — used for a duplicate close so the same bar can't
        place two live orders (Finding 2). A genuine close passes fire=True."""
        weekly, daily = store.load_weekly_daily_closed(self.conn, symbol)
        card = engine.evaluate(symbol, weekly, daily, self.profile, provisional=False)
        self.state.pair(symbol).confirmed = card
        self._compute_tranche(symbol, card)
        if fire and card.status == "BUY":
            ps = self.state.pair(symbol)
            if engine.is_stale(ps.tick_age(), STALE_SECS):
                log.info("F5: suppressing confirmed alert for %s — STALE (tick_age=%.0fs > %ds)",
                         symbol, ps.tick_age(), STALE_SECS)
            else:
                self._maybe_alert(symbol, card, kind="confirmed")
        return card

    def _recompute_regime(self):
        weekly, daily = store.load_weekly_daily_closed(self.conn, BTC_SYMBOL)
        wc, dc = weekly[3], daily[0]
        self.state.regime = engine.regime(wc, dc, self.profile)
        # Persist the regime label so the executor (which holds only the DB conn, not
        # AppState) can gate accumulation on it (config.ACCUMULATE_ONLY_IN_BEAR). Guarded
        # — a meta write must never break the regime recompute / writer.
        try:
            store.meta_set(self.conn, "regime", getattr(self.state.regime, "label", "UNKNOWN"))
        except Exception:
            log.exception("regime meta persist failed (regime state unaffected)")

    def _compute_tranche(self, symbol, card):
        """F8, computed here (not in engine.evaluate — that signature is locked
        by the M3 parity gate). Uses the LIVE tick price when available, else
        falls back to the last confirmed close — the champion card shows both,
        labeled. Called on every tick (cached pair info, pure arithmetic).
        Guarded: a sizing edge case must never kill the writer."""
        try:
            if symbol not in self._pair_info_cache:
                self._pair_info_cache[symbol] = store.get_pair_info(self.conn, symbol)
            info = self._pair_info_cache[symbol]
            if info is None or info["ordermin"] is None or info["costmin"] is None:
                return
            ps = self.state.pair(symbol)
            live = ps.last_tick.last if ps.last_tick else None
            price = live if live else card.price
            if not price or price <= 0:
                return
            qty, mult = engine.tranche(card.score, card.required, info["ordermin"],
                                        info["costmin"], info["lot_decimals"], price)
            ps.tranche = TrancheInfo(qty=qty, mult=mult, price=price, price_is_live=bool(live))
        except Exception:
            log.exception("tranche computation failed for %s (display-only, skipped)", symbol)

    def _maybe_recompute_provisional(self, symbol):
        ps = self.state.pair(symbol)
        now_mono = time.monotonic()
        if now_mono - ps.last_provisional_ts < 1.0:
            return None  # F13 throttle: <=1/s per pair

        fw = store.get_forming(self.conn, symbol, 10080)
        fd = store.get_forming(self.conn, symbol, 1440)
        if fw is None or fd is None:
            return None  # cold start: haven't seen both forming bars yet — don't
            # consume the throttle window on a miss, or the update that WOULD
            # complete the pair (arriving moments later) gets throttled away.
        ps.last_provisional_ts = now_mono

        weekly, daily = store.load_weekly_daily_closed(self.conn, symbol)
        wo, wh, wl, wc, wvol = weekly
        weekly_p = (wo + [fw["o"]], wh + [fw["h"]], wl + [fw["l"]], wc + [fw["c"]], wvol + [fw["v"]])
        daily_p = (daily[0] + [fd["c"]],)
        ef = _elapsed_fraction(fw["ts"], 10080)
        card = engine.evaluate(symbol, weekly_p, daily_p, self.profile, provisional=True, elapsed_fraction=ef)
        ps.provisional = card
        if PROVISIONAL_ALERTS and card.status == "BUY":
            self._maybe_alert(symbol, card, kind="provisional")
        return card

    def _maybe_arm_startup_buy(self, symbol, ps):
        """Fire a symbol that is ALREADY a confirmed BUY when this process starts.

        The alert/exec path is edge-triggered on a live candle *close*
        (_recompute_confirmed). startup_sweep() publishes the confirmed cards but
        deliberately does NOT fire — so a symbol sitting at BUY when the bot
        (re)starts would never place an order until its next daily/weekly close
        (up to 7 days). This one-shot arm closes that gap: on the first fresh tick
        for such a symbol, run the same _maybe_alert path a close would. Fired at
        most once per symbol per process (self._armed_buys); real closes re-fire
        independently. F10 cooldown still applies (with REALERT_HOURS=0 it fires;
        set >0 to let a recent alert suppress the boot re-fire). NOTE: because it
        re-arms every launch, each restart re-fires every open BUY — intended
        under the operator's cooldown-off/no-blockers stance."""
        card = ps.confirmed
        if (symbol in self._armed_buys or card is None or card.status != "BUY"
                or engine.is_stale(ps.tick_age(), STALE_SECS)
                # Only symbols that were BUY at BOOT may boot-arm. A symbol that became
                # BUY later (reconciler resweep / 'f'-key) is NOT here, so the quiet
                # resweep stays quiet; its real close fires it instead (Finding 3).
                or self._boot_buys is None or symbol not in self._boot_buys):
            return
        # If a pending entry bid already rests for this symbol, its thesis is already
        # expressed on the book — the arm's whole purpose (bridge the up-to-7-day wait
        # for the next close) is already served. Skip the re-fire so a restart doesn't
        # stack a redundant bid on the resting one; the duplicates were restart-driven,
        # not new pyramid steps. Consume the one-shot; the next real close fires normally.
        if store.has_pending_entry(self.conn, symbol):
            self._armed_buys.add(symbol)
            log.info("startup-arm: %s already has a resting pending entry — skipping re-fire", symbol)
            self._journal("order", symbol, "boot-arm skipped — entry already resting")
            return
        # Own each level once: if we already hold an OPEN rung within a ladder step of the
        # live price, the boot arm would just re-stack a near-market rung on EVERY restart
        # (it bids near the live tick, not at a dip) — a slow restart-driven drip. The
        # has_pending_entry check above covers a resting BID; this covers an already-FILLED
        # rung near price. Reuses the same guard continuous laddering uses. Only when an
        # executor exists (off mode is signal-only — no rungs to stack).
        live_px = ps.last_tick.last if ps.last_tick else None
        if (live_px and self.executor is not None
                and self.executor._owns_level_near(symbol, live_px, config.LADDER_STEP_PCT)):
            self._armed_buys.add(symbol)
            log.info("startup-arm: %s already owns a rung within a step of %.6g — skipping "
                     "re-fire (own each level once)", symbol, live_px)
            self._journal("order", symbol, "boot-arm skipped — own a rung near price")
            return
        self._armed_buys.add(symbol)   # one-shot: never retry this symbol on later ticks
        log.info("startup-arm: %s already confirmed BUY %d/%d — firing on first fresh tick",
                 symbol, card.score, card.denom)
        self._maybe_alert(symbol, card, kind="confirmed")

    def _maybe_alert(self, symbol, card, kind):
        now = time.time()
        last_ts = store.last_alert_ts(self.conn, symbol, kind)
        if not engine.should_alert(last_ts, now, REALERT_HOURS):
            log.info("cooldown suppresses %s alert for %s (last=%.0f, now=%.0f, gap=%.0fs < %ds)",
                     kind, symbol, last_ts, now, now - last_ts, REALERT_HOURS * 3600)
            return
        if kind == "confirmed":
            self._journal("detect", symbol, f"{card.score}/{card.denom} confirmed BUY")
        # SPEC §11: the alert message carries the LIVE price. The card's price
        # is the last closed daily close — hours stale at alert time.
        ps = self.state.pair(symbol)
        price = card.price
        if ps.last_tick is not None and not engine.is_stale(ps.tick_age(), STALE_SECS):
            price = ps.last_tick.last
        # The alert chain (sound/notify/Telegram) and live order placement do
        # BLOCKING network I/O with retries — on the writer that stalls the event
        # loop and can trip the WS watchdog. Offload to a thread (own DB conn,
        # sqlite isn't cross-thread) when a loop is running; run inline otherwise
        # (--once / tests, where self.conn is the right connection).
        do_exec = (kind == "confirmed" and self.executor is not None)
        args = (symbol, price, card.score, card.denom, list(card.fired), kind, do_exec, card)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._dispatch(self.conn, *args)     # sync path (no event loop)
            return
        # Hold a strong reference: asyncio only weakly tracks tasks, so an
        # unreferenced ensure_future can be GC'd before it runs — dropping the
        # alert/order. Keep it in a set until it completes.
        task = asyncio.ensure_future(asyncio.to_thread(self._dispatch_threaded, *args))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _dispatch_threaded(self, *args):
        conn = store.connect(config.DB_PATH)     # thread-local connection
        try:
            self._dispatch(conn, *args)
        except Exception:
            log.exception("offloaded alert/exec dispatch failed")
        finally:
            conn.close()

    def _dispatch(self, conn, symbol, price, score, denom, fired, kind, do_exec, card):
        # Place the live order FIRST and in isolation. The alert chain is DECORATION and
        # must never delay or, on a raise, silently DROP a live order — `alerter.fire`
        # runs unguarded DB/format code (e.g. a 'database is locked' on the alerts
        # insert, a Telegram timeout) and previously sat upstream of place_entry inside
        # the same try, so any such raise skipped the order. Order first; alert wrapped
        # so its failure is logged, never propagated (decoration must not kill the trade).
        if do_exec:
            from . import executor as executor_mod
            ex = executor_mod.Executor(conn)
            ex.mode = config.EXEC_MODE
            ex.place_entry(symbol, price, card)
        try:
            alerter.fire(conn, symbol, price, score, denom, fired, kind=kind)
        except Exception:
            log.exception("alerter.fire failed for %s (kind=%s) — order already handled; alert dropped",
                          symbol, kind)
```

## `deepfield/app.py` (534 lines)

```python
"""Live app wiring: warm backfill -> DB -> Ingest (startup sweep) -> dual WS ->
writer -> clock-close watchdog -> hourly reconciler -> UI (rich or --simple)
-> keys (q/p/f/a). SPEC §5/§8/§12.
"""
import asyncio
import logging
import datetime
import statistics
import time

from . import VERSION
from . import config
from . import store
from . import backfill
from . import reconciler
from . import rest_client
from . import ingest as ingest_mod
from . import ui
from . import simple_ui
from . import alerter
from .ws_client import WSClient
from .state import AppState
from .keys import KeyController
from .logsetup import setup_logging

log = logging.getLogger("deepfield.app")


def _make_gap_heal_cb(intervals):
    """Reconnect gap-heal scoped to the intervals that connection actually
    carries — conn A owns 1440, conn B owns 10080. Healing both from both
    doubled the REST load on every reconnect for no coverage gain."""
    def _heal():
        c = store.connect(config.DB_PATH)
        try:
            symbols = [p["ws"] for p in config.PAIRS]
            return reconciler.gap_heal(c, symbols, rest_client.fetch_ohlc, intervals=intervals)
        finally:
            c.close()

    async def gap_heal_cb(_syms):
        await asyncio.to_thread(_heal)
    return gap_heal_cb


def _make_ws_clients(symbols, queue):
    """Two connections (§6 discrepancy, M4): Kraken v2 allows one ohlc interval
    per symbol per connection. A = ticker+ohlc@1440 (30 subs), B = ohlc@10080
    (15 subs) — 45 subscriptions total, just not on one socket."""
    client_a = WSClient(symbols, queue,
                        subs=[{"channel": "ticker"}, {"channel": "ohlc", "interval": 1440}],
                        on_connect=_make_gap_heal_cb((1440,)), name="A(ticker+D)")
    client_b = WSClient(symbols, queue,
                        subs=[{"channel": "ohlc", "interval": 10080}],
                        on_connect=_make_gap_heal_cb((10080,)), name="B(W)")
    return [client_a, client_b]


def _heal_all():
    """Full-scope heal (both intervals) — hourly pass and the 'f' key."""
    c = store.connect(config.DB_PATH)
    try:
        symbols = [p["ws"] for p in config.PAIRS]
        return reconciler.gap_heal(c, symbols, rest_client.fetch_ohlc)
    finally:
        c.close()


def _poll_fills_threaded():
    """Off-loop (own conn): promote filled entry limits to positions and rest
    their stops. Blocking Kraken I/O, so never on the event loop."""
    from . import executor as executor_mod
    c = store.connect(config.DB_PATH)
    try:
        e = executor_mod.Executor(c)
        e.mode = "live"
        e.poll_fills()
    except Exception:
        log.exception("poll_fills failed")
    finally:
        c.close()


def _sys_journal(conn, text):
    """Isolated 'sys' journal emit for lifecycle events — never raises out."""
    try:
        store.journal(conn, "sys", "", text)
    except Exception:
        log.exception("sys journal emit failed — unaffected")


def _build_by_pair(conn, appstate):
    """v6 SURVEY: per-pair ledger snapshot for the FIELD bands + BOOK view.
    Pure DB read of the open/pending order rows, keyed by symbol. uP&L here is a
    snapshot convenience (last known tick); the FIELD LEDGER recomputes per-fill
    uP&L live at render from ps.last_tick (renderers own the live math)."""
    by_pair = {}
    for oid, sym, ts, vol, lev, entry, stop, stop_txid in conn.execute(
            "SELECT id, symbol, ts, volume, leverage, entry, stop, stop_txid FROM orders "
            "WHERE status='open' ORDER BY symbol, id"):
        d = by_pair.setdefault(sym, {"fills": [], "pendings": [], "vol_sum": 0.0,
                                     "avg_entry": None, "upnl": None, "stop": None})
        d["fills"].append({"id": oid, "ts": ts, "vol": vol, "lev": lev,
                           "entry": entry, "stop": stop, "stop_txid": stop_txid})
    for sym, price, vol, ts in conn.execute(
            "SELECT symbol, entry, volume, ts FROM orders "
            "WHERE status='pending' ORDER BY symbol, id"):
        d = by_pair.setdefault(sym, {"fills": [], "pendings": [], "vol_sum": 0.0,
                                     "avg_entry": None, "upnl": None, "stop": None})
        d["pendings"].append({"price": price, "vol": vol, "ts": ts})
    for sym, d in by_pair.items():
        fills = d["fills"]
        vsum = sum((f["vol"] or 0.0) for f in fills)
        d["vol_sum"] = vsum
        if vsum > 0:
            num = sum((f["vol"] or 0.0) * (f["entry"] or 0.0) for f in fills)
            d["avg_entry"] = num / vsum
        stops = [f["stop"] for f in fills if f["stop"] is not None]
        d["stop"] = max(stops) if stops else None   # tightest protective floor for the stack
        ps = appstate.pairs.get(sym)
        cur = ps.last_tick.last if (ps and ps.last_tick) else None
        if cur is not None and vsum > 0:
            d["upnl"] = sum((cur - (f["entry"] or 0.0)) * (f["vol"] or 0.0) for f in fills)
    return by_pair


def _snapshot_capacity(conn, appstate, free_margin):
    """Room to keep buying: free margin ÷ the typical fill's margin, in min-fills.
    Median margin of the last 10 LIVE fills; fallback to the mean of the current
    per-pair exec_plan margins. None when neither is available."""
    if not free_margin or free_margin <= 0:
        return None
    rows = conn.execute(
        "SELECT margin FROM orders WHERE mode='live' AND status IN('open','closed') "
        "AND margin IS NOT NULL ORDER BY id DESC LIMIT 10").fetchall()
    margins = [float(r[0]) for r in rows if r[0] is not None]
    if not margins:
        margins = [p.exec_plan["margin"] for p in appstate.pairs.values()
                   if p.exec_plan and p.exec_plan.get("margin")]
        if margins:
            margins = [sum(margins) / len(margins)]
    if not margins:
        return None
    typical = statistics.median(margins)
    return int(free_margin / typical) if typical > 0 else None


async def _exec_state_refresh(appstate, conn, ing, interval=15):
    """Publish the execution snapshot (equity/positions/rails) + per-BUY cooldown
    and dry-run order plan into AppState, so the UI stays a pure reader. Equity is
    the only slow bit (a Kraken call in live) — isolated to a worker thread; every
    DB touch stays on the loop (single-writer safe)."""
    import os
    import time as _t
    from . import broker
    ex = ing.executor
    while True:
        try:
            mode = config.EXEC_MODE
            balance = None
            margin_used = free_margin = None
            if ex is None:
                equity = None
            elif mode == "live":
                await asyncio.to_thread(_poll_fills_threaded)   # filled limits -> positions + stops
                balance = await asyncio.to_thread(broker.trade_balance_full)

                def _bf(key):   # each field independent — a missing m/mf must not null equity
                    try:
                        return float(balance[key]) if balance else None
                    except (TypeError, ValueError, KeyError):
                        return None
                # equity via the SHARED extractor (e->eb->tb) so the dashboard, rails,
                # peak, and the order path can never disagree on the sizing denominator.
                equity = broker.equity(balance)
                margin_used, free_margin = _bf("m"), _bf("mf")
                if equity:
                    ex._update_peak(equity)      # DB write, back on the loop
            else:
                equity = config.PAPER_PORTFOLIO_USD
                free_margin, margin_used = equity, 0.0
            rails_ok, reason = ex.rails_ok(equity) if ex else (True, "")
            positions = [
                {"symbol": r[0], "entry": r[1], "stop": r[2], "volume": r[3],
                 "leverage": r[4], "margin": r[5], "mode": r[6]}
                for r in conn.execute(
                    "SELECT symbol,entry,stop,volume,leverage,margin,mode FROM orders "
                    "WHERE status='open' ORDER BY id DESC")
            ]
            pending = [
                {"symbol": r[0], "entry": r[1], "volume": r[2], "leverage": r[3]}
                for r in conn.execute(
                    "SELECT symbol,entry,volume,leverage FROM orders "
                    "WHERE status='pending' ORDER BY id DESC")
            ]
            # v6 SURVEY read-only plumbing: per-pair ledger, journal tail, realized
            # day/week P&L (F6 boundaries, verbatim from rails_ok), min-fill capacity.
            now = datetime.datetime.now(datetime.timezone.utc)
            day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            wk0 = (now - datetime.timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0).isoformat()

            def _realized(since):        # display-only — never let it blank the snapshot
                try:
                    return store.realized_pnl_since(conn, since)
                except Exception:
                    log.exception("realized_pnl_since failed (display value only)")
                    return 0.0

            def _day_swing(eq):
                """Daily book SWING = equity now − equity at the first read of the UTC day.
                Complements 'day' (realized): realized only moves when something CLOSES (for
                a long-only, stops-only book that's $0 until a stop -> a loss), so the swing
                is the live mark-to-market move that actually tracks how the book did today.
                Baseline is persisted in meta, so it survives the day's restarts (best-effort:
                if the bot was down over midnight the baseline is the first read after).
                Display-only; None when equity is unknown."""
                if eq is None:
                    return None
                try:
                    today = now.date().isoformat()
                    d, _, base = (store.meta_get(conn, "day_open_equity") or "").partition("|")
                    if d != today or not base:
                        store.meta_set(conn, "day_open_equity", f"{today}|{eq}")
                        return 0.0
                    return eq - float(base)
                except Exception:
                    log.exception("day swing calc failed (display value only)")
                    return None
            # live stop coverage: open rows carrying a resting stop txid (header
            # safety-reading number — tracks the live book, not the boot recon stamp)
            scov = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN stop_txid IS NOT NULL "
                "AND stop_txid<>'' THEN 1 ELSE 0 END),0) FROM orders WHERE status='open'"
            ).fetchone()
            appstate.exec = {
                "mode": mode, "equity": equity, "open_count": len(positions),
                "positions": positions, "pending": pending,
                "rails_ok": rails_ok, "rails_reason": reason,
                "halt": os.path.exists(config.HALT_FILE), "updated": _t.time(),
                "balance": balance, "margin_used": margin_used, "free_margin": free_margin,
                "by_pair": _build_by_pair(conn, appstate),
                "journal_tail": store.recent_journal(conn, 200),
                "realized_day": _realized(day0),
                "realized_week": _realized(wk0),
                "swing_day": _day_swing(equity),   # live mark-to-market move since UTC midnight

                "capacity": _snapshot_capacity(conn, appstate, free_margin),
                "last_recon": store.meta_get(conn, "last_recon"),
                "stops_total": scov[0], "stops_covered": scov[1],
            }
            for sym, ps in list(appstate.pairs.items()):
                card = ps.confirmed
                if card and card.status == "BUY":
                    last = store.last_alert_ts(conn, sym, "confirmed")
                    ps.cooldown_until = (last + config.REALERT_HOURS * 3600) if last else 0.0
                    price = ps.last_tick.last if ps.last_tick else card.price
                    ps.exec_plan = ex.plan(sym, price, card, equity) if (ex and equity) else None
                else:
                    ps.cooldown_until = 0.0
                    ps.exec_plan = None
            # v6 web console: persist the broker-only values (equity/margin/tick
            # price/links) the read-only web server can't get from the DB. Pure
            # display persistence — no trading effect. Never blanks the snapshot.
            try:
                _persist_web_live(conn, appstate, equity, margin_used, free_margin, balance)
            except Exception:
                log.exception("web_live persist failed (display value only)")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("exec state refresh failed (will retry)")
        await asyncio.sleep(interval)


def _persist_web_live(conn, appstate, equity, margin_used, free_margin, balance):
    """Write the read-only `web_live` meta blob for deepfield.web.server. Broker-
    only fields (equity/margin/level/links/tick prices) that a DB reader can't see."""
    import json as _json
    import time as _t
    prices, chg = {}, {}
    for sym, ps in appstate.pairs.items():
        if ps.last_tick:
            prices[sym] = ps.last_tick.last
            cp = getattr(ps.last_tick, "change_pct", None)
            if cp is not None:
                chg[sym] = round(cp, 1)
    lvl = None
    try:
        lvl = float(balance["ml"]) if (balance and balance.get("ml")) else None
    except (TypeError, ValueError, KeyError):
        lvl = None
    links = ([bool(appstate.links[n].get("up")) for n in sorted(appstate.links)]
             if appstate.links else None)
    blob = {
        "equity": equity, "margin_used": margin_used, "free_margin": free_margin,
        "margin_level": round(lvl) if lvl else None,
        "capacity": appstate.exec.get("capacity"),
        "prices": prices, "chg": chg, "links": links,
        "mode": config.EXEC_MODE, "started": appstate.started_ts, "updated": _t.time(),
    }
    store.meta_set(conn, "web_live", _json.dumps(blob))
    if equity is not None:
        store.equity_snapshot(conn, equity)          # sparkline series, 5-min sampled


async def _hourly_reconciler(ing):
    while True:
        await asyncio.sleep(3600)
        try:
            repairs = await asyncio.to_thread(_heal_all)
            if repairs:
                # Repaired closed bars change the truth the cards were computed
                # from — republish. Quiet sweep (no alerts) by design.
                log.info("hourly reconcile made %d repairs — resweeping confirmed cards", repairs)
                ing.startup_sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hourly reconcile pass failed (will retry next hour)")


def _startup(debug, announce=False):
    """Shared by run_live and run_once: warm backfill -> DB -> startup sweep."""
    from . import broker
    setup_logging(debug=debug)
    broker.setup_raw_log(config.LOG_DIR)   # RAW order req/resp -> its own audit file
    if announce:
        print("DEEPFIELD warming up — backfilling candle gap (throttled REST)...", flush=True)
    # log=print would flood stdout ahead of every --once/--simple frame; route
    # backfill's per-series lines through logging instead, matching everything else.
    backfill.run(full=False, log=logging.getLogger("deepfield.backfill").info)
    conn = store.connect(config.DB_PATH)
    appstate = AppState()
    ing = ingest_mod.Ingest(conn, appstate)
    if announce:
        print("DEEPFIELD sweeping confirmed scores...", flush=True)
    ing.startup_sweep()
    # Persistence: on a live restart, surface any drift between our open-orders
    # ledger and what Kraken actually shows (a stop may have filled while down).
    if config.EXEC_MODE == "live" and ing.executor is not None:
        try:
            kr = broker.open_positions()
            ours = store.open_position_count(conn)
            # kr is None on an API failure — guard the len() so a transient blip in this
            # cosmetic log line can't raise and skip verify_open_stops() below (which has
            # its own None-handling and MUST run to re-place any missing/orphaned stops).
            log.info("startup position check: ledger open=%d · Kraken open positions=%s",
                     ours, len(kr) if kr is not None else "unavailable")
            ing.executor.verify_open_stops()   # re-place any missing protective stops
        except Exception:
            log.exception("startup position/stop check failed")
    log.info("startup sweep complete: %d pairs, regime=%s",
             len(config.PAIRS), appstate.regime.label if appstate.regime else "?")
    return conn, appstate, ing


def _start_web_console():
    """Serve the read-only web console in a daemon thread so one launch (the desktop
    icon) brings up TUI + web together. Fully isolated: its own ro DB connections, a
    guarded loop — it can never delay or crash the bot. Best-effort; a busy port just
    logs and moves on."""
    import threading

    def _run():
        try:
            from .web import server as web_server
            web_server.serve(port=config.WEB_PORT, quiet=True)
        except OSError as e:
            log.warning("web console not started (port %d in use?): %s", config.WEB_PORT, e)
        except Exception:
            log.exception("web console thread crashed (bot unaffected)")

    threading.Thread(target=_run, name="web-console", daemon=True).start()
    log.info("web console → http://127.0.0.1:%d", config.WEB_PORT)


async def run_live(simple=False, debug=False):
    log.info("DEEPFIELD starting (simple=%s)", simple)
    conn, appstate, ing = _startup(debug, announce=not simple)
    if config.WEB_ENABLED:
        try:
            _start_web_console()
        except Exception:
            log.exception("web console launch failed (bot continues)")
    _sys_journal(conn, f"process start — survey v{VERSION} · exec {config.EXEC_MODE}")
    symbols = [p["ws"] for p in config.PAIRS]
    queue = asyncio.Queue()

    clients = _make_ws_clients(symbols, queue)
    stop = asyncio.Event()
    heal_running = {"flag": False}

    def on_quit():
        log.info("key: q — shutting down")
        stop.set()

    def on_pause():
        appstate.paused = not appstate.paused
        appstate.pause_dirty = True
        log.info("key: p — render %s", "paused" if appstate.paused else "resumed")

    def on_force_reconcile():
        if heal_running["flag"]:
            log.info("key: f — reconcile already running, ignored")
            return
        log.info("key: f — forcing full reconcile")

        async def _run():
            heal_running["flag"] = True
            try:
                repairs = await asyncio.to_thread(_heal_all)
                log.info("forced reconcile complete: %d repairs", repairs)
                if repairs:
                    ing.startup_sweep()
            finally:
                heal_running["flag"] = False
        asyncio.ensure_future(_run())

    def on_test_alert():
        log.info("key: a — test alert")

        def _fire():
            c = store.connect(config.DB_PATH)  # thread-local conn, never the writer's
            try:
                alerter.test_alert(c)
            finally:
                c.close()
        asyncio.ensure_future(asyncio.to_thread(_fire))

    # ── v6 SURVEY view controls — mutate AppState only, then wake the renderer ──
    appstate._key_evt = asyncio.Event()   # run_ui waits on this for instant redraw

    def _wake():
        appstate.pause_dirty = True        # so a keypress redraws even while paused
        appstate._key_evt.set()

    def on_view(n):
        return lambda: (ui.nav_view(appstate, n), _wake())

    def on_select(delta):
        return lambda: (ui.nav_select(appstate, delta), _wake())

    def on_expand():
        ui.nav_expand(appstate)
        _wake()

    def on_scroll(delta):
        return lambda: (ui.nav_scroll(appstate, delta), _wake())

    keys = KeyController(asyncio.get_running_loop(), {
        b"q": on_quit, b"p": on_pause, b"f": on_force_reconcile, b"a": on_test_alert,
        b"1": on_view(1), b"2": on_view(2), b"3": on_view(3),
        b"j": on_select(1), b"k": on_select(-1),
        b"\x1b[B": on_select(1), b"\x1b[A": on_select(-1),   # ↓ / ↑
        b"\r": on_expand, b"\n": on_expand, b" ": on_expand,
        b",": on_scroll(-1), b".": on_scroll(1),
    })
    keys_active = keys.start() if not simple else False

    tasks = [asyncio.ensure_future(c.run()) for c in clients]
    tasks.append(asyncio.ensure_future(ing.run(queue)))
    tasks.append(asyncio.ensure_future(ing.clock_close_watchdog(rest_client.fetch_ohlc)))
    tasks.append(asyncio.ensure_future(_hourly_reconciler(ing)))
    tasks.append(asyncio.ensure_future(_exec_state_refresh(appstate, conn, ing)))
    tasks.append(asyncio.ensure_future(
        simple_ui.run_simple(appstate, conn) if simple
        else ui.run_ui(appstate, conn, show_keys=keys_active)
    ))

    stop_task = asyncio.ensure_future(stop.wait())
    try:
        done, _pending = await asyncio.wait([stop_task, *tasks],
                                            return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is not stop_task and t.exception() is not None:
                log.error("task died: %r", t.exception())
    finally:
        keys.stop()
        stop_task.cancel()
        for c in clients:
            await c.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _sys_journal(conn, "process stop — clean shutdown")
        conn.close()
        log.info("DEEPFIELD stopped cleanly")


def run_once(debug=False):
    """--once: single confirmed evaluation + one plaintext frame (cron/tests)."""
    conn, appstate, ing = _startup(debug)
    print(simple_ui.render_frame_text(appstate, conn))
    conn.close()


def run_exec_probe(debug=False):
    """--exec-probe: send validate=true orders for all 15 pairs against real
    Kraken — proves pair name, leverage, precision, and minimums are accepted
    WITHOUT executing. The proof gate before EXEC_MODE goes live."""
    from . import broker, executor
    setup_logging(debug=debug)
    broker.setup_raw_log(config.LOG_DIR)
    if not broker.keys_present():
        print(f"NO KEYS — put your Kraken key/secret (2 lines) in {broker.KEYFILES[0]} first.")
        print("Use a DEDICATED API key for DEEPFIELD (nonce is per-key; sharing with hydra collides).")
        return
    backfill.run(full=False, log=logging.getLogger("deepfield.backfill").info)
    conn = store.connect(config.DB_PATH)
    appstate = AppState()
    ing = ingest_mod.Ingest(conn, appstate)
    ing.startup_sweep()
    ex = executor.Executor(conn)
    ex.mode = "validate"
    print("VALIDATE PROBE — real Kraken order-check, nothing executes:\n")
    for p in config.PAIRS:
        sym = p["ws"]
        ps = appstate.pair(sym)
        card = ps.confirmed
        price = card.price if card else None
        if not price:
            print(f"  {sym:9s} skip (no price)")
            continue
        oid = ex.place_entry(sym, price, card)
        row = conn.execute("SELECT status, entry, stop, volume, leverage, error FROM orders WHERE id=?",
                           (oid,)).fetchone() if oid else None
        if row:
            st, entry, stop, vol, lev, err = row
            mark = "✅" if st == "validated" else "❌"
            print(f"  {sym:9s} {mark} {st:9s} vol={vol:g} x{lev} @ {entry} stop={stop}" + (f"  {err}" if err else ""))
        else:
            print(f"  {sym:9s} ❌ no order row")
    conn.close()
```

## `deepfield/signals.py` (188 lines)

```python
"""The seven Oracle signals — pure functions, zero I/O. SPEC §7 + docs/RULINGS.md.

Each signal returns a SignalResult keyed by SLOT (1..7), not display string, so
the sig5 rename never ghosts a parity diff (reviewer #3). Tri-state:
FIRED / NOT / NA. NA (F3, only under profile.f3) shrinks the denominator and can
never inflate a score (invariant 4); in compat, insufficient data -> NOT, matching
v4.4 which counts it in the fixed denominator of 7.

Indicator inputs (w_ema200, w_rsi, w_hist, d_rsi, w_vol_sma) are computed ONCE by
the engine and passed in, matching v4.4's single inline computation.
"""
from dataclasses import dataclass

from .config import DOWN_WEEKS, PIVOT_MIN_DEPTH

FIRED = "fired"
NOT = "not"
NA = "na"

# Slot -> (compat name, full name). Only slot 5 differs (F1 rename).
NAMES = {
    1: ("Below W-EMA200", "Below W-EMA200"),
    2: ("W-RSI<40 Turning Up", "W-RSI<40 Turning Up"),
    3: ("W-MACD Hist Crossup", "W-MACD Hist Crossup"),
    4: ("D-RSI Divergence", "D-RSI Divergence"),
    5: ("W-First Higher High", "W-First Up Close"),
    6: ("W-Vol Accumulation", "W-Vol Accumulation"),
    7: ("Near 52w Low (<20%)", "Near 52w Low (<20%)"),
}


@dataclass
class SignalResult:
    slot: int
    name: str
    state: str          # FIRED | NOT | NA
    reason: str = ""    # populated on NA


def _name(slot, profile):
    compat_name, full_name = NAMES[slot]
    return full_name if (slot == 5 and profile.f1) else compat_name


def _mk(slot, profile, fired, na=False, reason=""):
    if na:
        return SignalResult(slot, _name(slot, profile), NA, reason)
    return SignalResult(slot, _name(slot, profile), FIRED if fired else NOT)


# ── sig1: price below weekly EMA200 (F3 makes <200 bars N/A) ────────────────
def sig1_below_wema200(wc, w_ema200, profile):
    if len(wc) < 200 or w_ema200[-1] <= 0:
        if profile.f3:
            return _mk(1, profile, False, na=True, reason="needs 200 weekly bars")
        return _mk(1, profile, False)  # v4.4: False, still in denominator
    return _mk(1, profile, wc[-1] < w_ema200[-1])


# ── sig2: weekly RSI < 40 and turning up vs 3 bars back (verbatim) ──────────
def sig2_wrsi_turning_up(w_rsi, profile):
    if not (w_rsi and w_rsi[-1] > 0) or len(w_rsi) < 5:
        if profile.f3:
            return _mk(2, profile, False, na=True, reason="needs 5 weekly RSI bars")
        return _mk(2, profile, False)
    ref = w_rsi[-4] if w_rsi[-4] > 0 else None
    if ref is None:
        return _mk(2, profile, False)
    return _mk(2, profile, (w_rsi[-1] < 40.0) and (w_rsi[-1] > ref))


# ── sig3: weekly MACD hist positive, was negative within 8 bars (verbatim) ──
def sig3_macd_crossup(w_hist, profile):
    if len(w_hist) < 4:
        if profile.f3:
            return _mk(3, profile, False, na=True, reason="needs 4 weekly MACD bars")
        return _mk(3, profile, False)
    now_pos = w_hist[-1] > 0
    lookback = w_hist[-8:] if len(w_hist) >= 8 else w_hist
    had_neg = any(h < 0 for h in lookback[:-1])
    return _mk(3, profile, had_neg and now_pos)


# ── sig4: daily RSI bullish divergence (F2 adds pivot quality) ──────────────
def _pivots(vals, min_depth, min_spacing):
    """3-bar local minima; F2: prominence >= min_depth vs BOTH neighbors
    (proportional) and accepted pivots >= min_spacing bars apart."""
    lows = []
    last_i = None
    for i in range(1, len(vals) - 1):
        v, a, b = vals[i], vals[i - 1], vals[i + 1]
        if not (v < a and v < b):
            continue
        if min_depth is not None and v > 0:
            if (a - v) / v < min_depth or (b - v) / v < min_depth:
                continue
        if min_spacing is not None and last_i is not None and (i - last_i) < min_spacing:
            continue
        lows.append((i, v))
        last_i = i
    return lows


def find_bullish_divergence(closes, rsi_vals, lookback=60, min_depth=None, min_spacing=None):
    """Price lower-low + RSI higher-low in the last `lookback` bars.
    v4.4 structure; F2 params add price-pivot prominence + spacing."""
    from .indicators import clean
    c = clean(closes[-lookback:])
    r = clean(rsi_vals[-lookback:])
    n = len(c)
    if n < 20:
        return False
    price_lows = _pivots(c, min_depth, min_spacing)
    # RSI pivots keep the v4.4 detector (spacing applied, no proportional depth).
    rsi_lows = []
    last_ri = None
    for i in range(1, n - 1):
        if r[i] > 0 and r[i] < r[i - 1] and r[i] < r[i + 1]:
            if min_spacing is not None and last_ri is not None and (i - last_ri) < min_spacing:
                continue
            rsi_lows.append((i, r[i]))
            last_ri = i
    if len(price_lows) < 2 or len(rsi_lows) < 2:
        return False
    p1_i, p1 = price_lows[-2]
    p2_i, p2 = price_lows[-1]
    r1 = r2 = None
    for ri, rv in rsi_lows:
        if abs(ri - p1_i) <= 5:
            r1 = rv
        if abs(ri - p2_i) <= 5:
            r2 = rv
    if r1 is None or r2 is None:
        return False
    return (p2 < p1) and (r2 > r1)


def sig4_drsi_divergence(dc, d_rsi, profile):
    if len(dc) < 21:  # find_bullish_divergence needs a >=20-bar window
        if profile.f3:
            return _mk(4, profile, False, na=True, reason="needs 20 daily bars")
        return _mk(4, profile, False)
    if profile.f2:
        fired = find_bullish_divergence(dc, d_rsi, 60, PIVOT_MIN_DEPTH, 3)
    else:
        fired = find_bullish_divergence(dc, d_rsi, 60)
    return _mk(4, profile, fired)


# ── sig5: first weekly up-close after a downtrend (F1 tightens) ─────────────
def sig5_first_up_close(wc, profile):
    need = (DOWN_WEEKS + 2) if profile.f1 else 5
    if len(wc) < need:
        if profile.f3:
            return _mk(5, profile, False, na=True, reason=f"needs {need} weekly bars")
        return _mk(5, profile, False)
    first_up = wc[-1] > wc[-2]
    if profile.f1:
        # DOWN_WEEKS consecutive lower closes immediately prior.
        downtrend = all(wc[-i - 1] < wc[-i - 2] for i in range(1, DOWN_WEEKS + 1))
    else:
        downtrend = (wc[-2] < wc[-3]) or (wc[-3] < wc[-4])
    return _mk(5, profile, first_up and downtrend)


# ── sig6: volume > SMA20 on a green/hammer weekly candle (verbatim) ─────────
def sig6_vol_accumulation(wo, wh, wl, wc, wvol, w_vol_sma, profile):
    if len(wvol) < 21 or w_vol_sma[-1] <= 0 or not (wo and wh and wl):
        if profile.f3:
            return _mk(6, profile, False, na=True, reason="needs 21 weekly bars")
        return _mk(6, profile, False)
    vol_above = wvol[-1] > w_vol_sma[-1]
    o, h, l, c = wo[-1], wh[-1], wl[-1], wc[-1]
    body = abs(c - o)
    is_green = c > o
    lower_wick = min(o, c) - l
    upper_body = h - max(o, c)
    is_hammer = (lower_wick > body * 1.5) and (upper_body < body * 0.5)
    return _mk(6, profile, vol_above and (is_green or is_hammer))


# ── sig7: price within 20% of 52-week low (verbatim) ───────────────────────
def sig7_near_52w_low(price, low_52w, profile):
    if not (low_52w and low_52w > 0 and price > 0):
        if profile.f3:
            return _mk(7, profile, False, na=True, reason="no 52w low")
        return _mk(7, profile, False)
    return _mk(7, profile, (price - low_52w) / low_52w <= 0.20)
```

## `deepfield/store.py` (428 lines)

```python
"""SQLite (WAL) persistence — SINGLE WRITER. SPEC §9, invariants 1 & 2.

Exactly one task/connection owns writes; everyone else uses the read helpers.
SQLite-WAL is ground truth; RAM-only state is a bug.

SINGLE WRITER is now ENFORCED, not just documented. The bot runs several writer
connections in one process — the ingest thread (candles), the off-loop poll_fills
thread (orders/stops), and the gap-heal threads (backfilled candles) — and WAL
lets only ONE connection hold an open write transaction at a time; the rest used
to collide as `database is locked` (chronic; a flood during the 2026-07-13 storm).
`_WriterConn` funnels every write through one process-global RLock: it grabs the
lock on a connection's first write statement and releases it on commit/rollback/
close, so the lock spans the whole transaction and no two connections ever write
at once. Readers (WAL-concurrent) never take the lock. busy_timeout stays as a
backstop for out-of-process writers (e.g. a --reconcile CLI run alongside live).

`candles.pair` holds the **v2 ws_symbol** (e.g. "BTC/USD"), so REST backfill (M1)
and WS live updates (M4) write the same rows. `ts` = bar OPEN (unix).
Close predicate (M1 sharpening): a bar is closed iff now >= ts + interval*60.
"""
import sqlite3
import threading
import time
import datetime

# Process-global write serializer. RLock (not Lock) so a thread that somehow
# re-enters a write path can't self-deadlock; the per-connection _holds_write
# guard keeps acquire/release balanced (one net acquire per open transaction).
_WRITE_LOCK = threading.RLock()
_WRITE_VERBS = frozenset(("INSERT", "UPDATE", "DELETE", "REPLACE",
                          "CREATE", "ALTER", "DROP"))


def _is_write(sql):
    """True if the statement opens/extends a write transaction (leading verb).
    SELECT/PRAGMA/WITH-read stay lock-free so readers never serialize."""
    s = sql.lstrip()
    if not s:
        return False
    return s.split(None, 1)[0].upper() in _WRITE_VERBS


class _WriterConn(sqlite3.Connection):
    """A connection that holds _WRITE_LOCK for the duration of any write
    transaction. All writes in this codebase go through conn.execute(...) (no
    cursors, no `with conn:`, no explicit BEGIN — verified), so intercepting
    execute/commit here covers every write with no call-site changes."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._holds_write = False

    def _acquire(self):
        if not self._holds_write:
            _WRITE_LOCK.acquire()
            self._holds_write = True

    def _release(self):
        if self._holds_write:
            self._holds_write = False
            _WRITE_LOCK.release()

    def execute(self, sql, *a, **k):
        if _is_write(sql):
            self._acquire()
        return super().execute(sql, *a, **k)

    def executemany(self, sql, *a, **k):
        if _is_write(sql):
            self._acquire()
        return super().executemany(sql, *a, **k)

    def executescript(self, script):
        # A script may contain writes (schema/migrations); hold the lock across it.
        self._acquire()
        return super().executescript(script)

    def commit(self):
        try:
            super().commit()
        finally:
            self._release()

    def rollback(self):
        try:
            super().rollback()
        finally:
            self._release()

    def close(self):
        # Backstop: releases the lock if a write path errored before commit,
        # so an abandoned open transaction can never wedge every other writer.
        try:
            super().close()
        finally:
            self._release()

# §9 schema + Q2 ruling: pairs gains lot_decimals (AssetPairs). ts = bar OPEN.
SCHEMA = """
CREATE TABLE IF NOT EXISTS candles(
    pair TEXT, interval INTEGER, ts INTEGER,        -- ts = bar OPEN, unix
    o REAL, h REAL, l REAL, c REAL, v REAL,
    closed INTEGER,
    PRIMARY KEY (pair, interval, ts)
);
CREATE TABLE IF NOT EXISTS pairs(
    rest_pair TEXT PRIMARY KEY, ws_symbol TEXT, display TEXT,
    ordermin REAL, costmin REAL, lot_decimals INTEGER, updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT,
    price REAL, score INTEGER, denom INTEGER, signals TEXT,
    kind TEXT                                       -- confirmed | provisional | test
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS journal(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, symbol TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, margin_pair TEXT,
    side TEXT, ordertype TEXT, mode TEXT,        -- off | paper | live | validate
    entry REAL, stop REAL, volume REAL, leverage INTEGER,
    notional REAL, margin REAL, risk_usd REAL,
    score INTEGER, required INTEGER,             -- entry conviction (rides down the ladder chain)
    txid TEXT, stop_txid TEXT, status TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS equity_history(
    ts INTEGER PRIMARY KEY,                      -- unix, sampled ~5min by the bot loop
    equity REAL                                  -- display-only (web sparkline)
);
"""


def _ensure_columns(conn, table, coldefs):
    """Idempotent additive migration (SQLite has no ADD COLUMN IF NOT EXISTS):
    ALTER-add any column in coldefs the live table is missing, leaving existing
    rows NULL for it. coldefs: [(name, decl), ...]. New/additive only — never
    drops or retypes, so it's safe to run on every connect."""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, decl in coldefs:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def connect(db_path):
    """Open the DB with WAL, init schema, return the connection.

    busy_timeout matters from M6 on: the live writer and the gap-heal thread
    (its own connection, per M4) now contend on the same WAL file on every
    (re)connect. Without it, a concurrent write throws "database is locked"
    instead of waiting the other side out.

    30s (was 5s): the per-tick candle writer got starved past 5s during WRITE
    BURSTS — a boot reconcile re-arming dozens of stops, or a seeding pass —
    each of which serially hammers the executor's connection for 10-30s while
    also driving frequent WAL auto-checkpoints. 5s expired mid-burst and skipped
    candle writes (thousands during the 2026-07-13 flatten storm; a smaller boot
    burst every restart). 30s waits any realistic burst out; steady state never
    contends, so it never actually blocks that long. A lock still held past 30s
    is a real bug worth surfacing, not masking further.
    """
    conn = sqlite3.connect(db_path, factory=_WriterConn)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        # Additive migration for DBs created before the orders conviction columns
        # (existing rows -> NULL score/required -> ladder falls back to flat min).
        # userref (audit 2026-07-13): our client id sent on every live AddOrder so an
        # order whose transport failed AMBIGUOUSLY (may or may not have landed) can be
        # re-identified on Kraken instead of becoming an untracked naked position.
        _ensure_columns(conn, "orders", [("score", "INTEGER"), ("required", "INTEGER"),
                                         ("userref", "INTEGER")])
        conn.commit()
    except Exception:
        conn.close()   # _WriterConn.close() frees the write lock the schema writes took
        raise
    return conn


def upsert_pair(conn, rest_pair, ws_symbol, display, ordermin, costmin, lot_decimals):
    conn.execute(
        """INSERT INTO pairs(rest_pair, ws_symbol, display, ordermin, costmin, lot_decimals, updated_ts)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(rest_pair) DO UPDATE SET
             ws_symbol=excluded.ws_symbol, display=excluded.display,
             ordermin=excluded.ordermin, costmin=excluded.costmin,
             lot_decimals=excluded.lot_decimals, updated_ts=excluded.updated_ts""",
        (rest_pair, ws_symbol, display, ordermin, costmin, lot_decimals, int(time.time())),
    )


def upsert_candle(conn, pair, interval, ts, o, h, l, c, v, closed):
    conn.execute(
        """INSERT INTO candles(pair, interval, ts, o, h, l, c, v, closed)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(pair, interval, ts) DO UPDATE SET
             o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v,
             closed=excluded.closed""",
        (pair, interval, ts, o, h, l, c, v, closed),
    )


def max_ts(conn, pair, interval):
    row = conn.execute(
        "SELECT MAX(ts) FROM candles WHERE pair=? AND interval=?", (pair, interval)
    ).fetchone()
    return row[0]


def max_closed_ts(conn, pair, interval):
    """MAX(ts) of a CLOSED bar for (pair, interval), or None. Seeds the per-close
    fire-dedup at boot so a restart doesn't re-fire a bar that already closed (and
    fired) before the restart."""
    row = conn.execute(
        "SELECT MAX(ts) FROM candles WHERE pair=? AND interval=? AND closed=1",
        (pair, interval),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def candle_count(conn, pair, interval):
    row = conn.execute(
        "SELECT COUNT(*) FROM candles WHERE pair=? AND interval=?", (pair, interval)
    ).fetchone()
    return row[0]


def flip_closed(conn, pair, interval, ts):
    """Mark a bar closed (0->1) without touching its OHLCV (already kept current
    by CandleUpdate upserts). Returns rowcount — 0 means the row wasn't there yet,
    which the caller should treat as a gap for the reconciler, not a crash."""
    cur = conn.execute(
        "UPDATE candles SET closed=1 WHERE pair=? AND interval=? AND ts=? AND closed=0",
        (pair, interval, ts),
    )
    return cur.rowcount


def load_weekly_daily_closed(conn, symbol):
    """Closed-only series shaped for engine.evaluate(): weekly=(wo,wh,wl,wc,wvol),
    daily=(dc,). Invariant 3 — the engine reads persisted state, not the stream."""
    w = conn.execute(
        "SELECT o,h,l,c,v FROM candles WHERE pair=? AND interval=10080 AND closed=1 ORDER BY ts",
        (symbol,),
    ).fetchall()
    d = conn.execute(
        "SELECT c FROM candles WHERE pair=? AND interval=1440 AND closed=1 ORDER BY ts",
        (symbol,),
    ).fetchall()
    weekly = ([r[0] for r in w], [r[1] for r in w], [r[2] for r in w], [r[3] for r in w], [r[4] for r in w])
    daily = ([r[0] for r in d],)
    return weekly, daily


def get_forming(conn, symbol, interval):
    """The current forming (closed=0) bar for symbol/interval, or None."""
    row = conn.execute(
        "SELECT ts,o,h,l,c,v FROM candles WHERE pair=? AND interval=? AND closed=0",
        (symbol, interval),
    ).fetchone()
    if row is None:
        return None
    return {"ts": row[0], "o": row[1], "h": row[2], "l": row[3], "c": row[4], "v": row[5]}


def insert_alert(conn, ts_iso, symbol, price, score, denom, signals, kind):
    conn.execute(
        "INSERT INTO alerts(ts, symbol, price, score, denom, signals, kind) VALUES(?,?,?,?,?,?,?)",
        (ts_iso, symbol, price, score, denom, "|".join(signals), kind),
    )
    conn.commit()


def insert_order(conn, row):
    """row: dict of the orders columns. Returns the new order id."""
    cols = ["ts", "symbol", "margin_pair", "side", "ordertype", "mode", "entry", "stop",
            "volume", "leverage", "notional", "margin", "risk_usd", "score", "required",
            "txid", "stop_txid", "status", "error", "userref"]
    cur = conn.execute(
        f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        [row.get(c) for c in cols],
    )
    conn.commit()
    return cur.lastrowid


def open_position_count(conn, mode=None):
    """Positions this bot opened and believes are open (status='open').
    mode (audit 2026-07-13 M6): scope to one exec mode so paper rows mixed into
    this DB can never leak into live money-path decisions. None = all modes
    (legacy display callers)."""
    if mode:
        return conn.execute("SELECT COUNT(*) FROM orders WHERE status='open' AND mode=?",
                            (mode,)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM orders WHERE status='open'").fetchone()[0]


def committed_position_count(conn, mode=None):
    """Filled positions PLUS resting entry limits ('pending') — the count the
    MAX_OPEN_POSITIONS rail must use, since every pending limit can still fill.
    mode: scope to one exec mode (see open_position_count)."""
    if mode:
        return conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status IN ('open','pending') AND mode=?",
            (mode,)).fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM orders WHERE status IN ('open','pending')").fetchone()[0]


def has_pending_entry(conn, symbol, mode=None):
    """True if this symbol already has a resting (status='pending') entry order — its
    BUY thesis is already expressed on the book, so the boot arm needn't re-fire it.
    mode: scope to one exec mode (see open_position_count)."""
    if mode:
        return conn.execute(
            "SELECT 1 FROM orders WHERE symbol=? AND status='pending' AND mode=? LIMIT 1",
            (symbol, mode)).fetchone() is not None
    return conn.execute(
        "SELECT 1 FROM orders WHERE symbol=? AND status='pending' LIMIT 1", (symbol,)
    ).fetchone() is not None


def realized_pnl_since(conn, since_iso):
    """Realized P&L for positions CLOSED since `since_iso`. Buckets by the close time
    (error.closed_ts), NOT the entry ts — the daily/weekly loss caps ask "how much did
    I lose since day0/wk0", a realization-date question (a trade entered days ago and
    stopped out today must count toward today). Rows without a recorded pnl/closed_ts
    (manual closes, unresolved) contribute nothing.

    The `error` column is polymorphic — JSON for stop-exit P&L, but plain text for
    manual/other closes (e.g. 'closed manually by operator'). json_extract RAISES
    'malformed JSON' on a non-JSON value, which crashes the whole query and every
    caller (the rails loss caps AND the v6 exec snapshot). Guard with json_valid so
    a plain-text row is simply skipped — matching the documented 'contributes
    nothing' intent instead of exploding."""
    row = conn.execute(
        "SELECT COALESCE(SUM(CAST(json_extract(error,'$.pnl') AS REAL)),0) FROM orders "
        "WHERE status='closed' AND json_valid(error)=1 AND json_extract(error,'$.closed_ts') >= ?",
        (since_iso,),
    ).fetchone()
    return row[0] if row else 0.0


def equity_snapshot(conn, equity, min_gap_s=300, keep_days=90):
    """Append a display-only equity sample (web sparkline series), at most one
    per min_gap_s; prunes samples older than keep_days. Never raises past the
    caller's display-only guard — no trading effect."""
    now = int(time.time())
    row = conn.execute("SELECT MAX(ts) FROM equity_history").fetchone()
    if row and row[0] and now - row[0] < min_gap_s:
        return
    conn.execute("INSERT OR REPLACE INTO equity_history(ts,equity) VALUES(?,?)",
                 (now, float(equity)))
    conn.execute("DELETE FROM equity_history WHERE ts < ?", (now - keep_days * 86400,))
    conn.commit()


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    conn.commit()


def get_pair_info(conn, ws_symbol):
    """F8 needs live ordermin/costmin/lot_decimals — read from the pairs table
    (populated from AssetPairs at backfill), keyed by ws_symbol here."""
    row = conn.execute(
        "SELECT ordermin, costmin, lot_decimals, display FROM pairs WHERE ws_symbol=?",
        (ws_symbol,),
    ).fetchone()
    if row is None:
        return None
    return {"ordermin": row[0], "costmin": row[1], "lot_decimals": row[2], "display": row[3]}


def recent_alerts(conn, n=5):
    """Last n ledger rows, newest first — for the UI alert tail (§8)."""
    return conn.execute(
        "SELECT ts, symbol, price, score, denom, signals, kind FROM alerts ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()


def journal(conn, kind, symbol, text):
    """Append one display-truth event to the journal (v6 SURVEY, JOURNAL view).

    DISPLAY-ONLY: the alerts table stays cooldown ground truth; this narrates the
    system for the operator's eyes. This raw writer commits and MAY raise (locked
    DB, disk). Every emit site in the money path MUST call it through a try/except
    wrapper (executor._journal / ingest._journal) so a journal failure can NEVER
    delay or drop a fill, stop, or order — the same isolation rule as the
    dispatch/alerter fix. Regression: test_journal_failure_never_blocks_fill."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute("INSERT INTO journal(ts, kind, symbol, text) VALUES(?,?,?,?)",
                 (ts, kind, symbol, text))
    conn.commit()


def recent_journal(conn, n=200):
    """Last n journal rows, newest first — the UI JOURNAL view + FIELD 'latest'."""
    return conn.execute(
        "SELECT ts, kind, symbol, text FROM journal ORDER BY id DESC LIMIT ?", (n,),
    ).fetchall()


def last_alert_ts(conn, symbol, kind="confirmed"):
    """F10: unix seconds of the most recent alert of `kind` for symbol, or None.
    Disk (this table) is ground truth for the cooldown — survives restarts.
    kind='confirmed' is the spec's F10 ledger; 'provisional' reuses the same
    per-symbol cooldown mechanism when PROVISIONAL_ALERTS is enabled."""
    row = conn.execute(
        "SELECT ts FROM alerts WHERE symbol=? AND kind=? ORDER BY ts DESC LIMIT 1",
        (symbol, kind),
    ).fetchone()
    if row is None:
        return None
    import datetime
    dt = datetime.datetime.fromisoformat(row[0])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()
```

