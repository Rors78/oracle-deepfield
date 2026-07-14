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
