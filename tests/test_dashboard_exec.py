"""Dashboard execution-integration: plan(), actionability, cooldown gating,
and that the frame renders the exec regions without exploding."""
import time

from deepfield import store, config, ui, executor as ex_mod
from deepfield.state import AppState, PairState


SYM = "BTC/USD"


class Card:
    def __init__(self, status="BUY", low_52w=58000.0):
        self.status = status
        self.low_52w = low_52w
        self.high_52w = 130000.0
        self.price = 63000.0
        self.score = 5
        self.denom = 7
        self.required = 5
        self.pct_above_low = 8.0
        self.weekly_rsi = 31.0
        self.wrsi_ref = 30.0
        self.daily_rsi = 53.0
        self.results = []
        self.gap = {}


def test_plan_is_pure_min_size_default(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    e = ex_mod.Executor(conn)
    plan = e.plan(SYM, 63000.0, Card(), 1000.0)
    assert plan["leverage"] == 10             # fixed, hardcoded (not derived)
    assert plan["size_mode"] == "min"         # default: minimum order
    assert plan["volume"] >= 0.00005 and plan["margin"] > 0
    assert plan["stop"] < 63000.0
    conn.close()


def _actionable_state(cooldown_until=0.0, stale=False):
    st = AppState()
    ps = st.pair(SYM)
    ps.confirmed = Card()
    ps.cooldown_until = cooldown_until
    ps.last_tick_ts = 0.0 if stale else time.time()
    ps.last_tick = type("T", (), {"last": 63000.0})()
    return st, ps


def test_actionable_true_when_fresh_and_uncooled():
    st, ps = _actionable_state()
    assert ui._actionable(ps, time.time()) is True


def test_actionable_false_when_cooldown_gated():
    st, ps = _actionable_state(cooldown_until=time.time() + 3600)
    assert ui._actionable(ps, time.time()) is False


def test_actionable_false_when_stale():
    st, ps = _actionable_state(stale=True)
    assert ui._actionable(ps, time.time()) is False


def test_exec_line_off_vs_live():
    st = AppState()
    assert "exec off" in ui.render_exec_line(st).plain           # v6 header status line
    st.exec = dict(st.exec)
    st.exec.update({"mode": "live", "equity": 1234.5, "open_count": 2,
                    "positions": [], "pending": [], "rails_ok": True,
                    "rails_reason": "", "halt": False})
    line = ui.render_exec_line(st).plain
    assert "exec LIVE" in line and "$1,234.50" in line and "2 pos" in line


def test_exec_line_shows_halt():
    st = AppState()
    st.exec = {"mode": "live", "equity": 100.0, "open_count": 0, "positions": [],
               "rails_ok": False, "rails_reason": "x", "halt": True, "updated": 0.0}
    assert "HALTED" in ui.render_exec_line(st).plain


def _tick(last, chg=0.5):
    return type("T", (), {"last": last, "change_pct": chg})()


def _bp(sym, entry, stop, vol, lev=10):
    return {sym: {"fills": [{"id": 1, "ts": "2026-07-05T00:00:00+00:00", "vol": vol,
                             "lev": lev, "entry": entry, "stop": stop, "stop_txid": "OSTOPX"}],
                  "pendings": [], "vol_sum": vol, "avg_entry": entry,
                  "upnl": None, "stop": stop}}


def test_frame_renders_position_in_field_and_book(tmp_path):
    """v6: an open position renders on the FIELD band (fills • + cursor) and in
    BOOK (per-fill ledger). Exec mode + counts show in the header status line."""
    conn = store.connect(str(tmp_path / "t.db"))
    st = AppState()
    now = time.time()
    ps = st.pair(SYM)
    ps.confirmed = Card()                     # BUY, low_52w 58000, high_52w 130000
    ps.last_tick = _tick(63000.0); ps.last_tick_ts = now
    st.exec = dict(st.exec)
    st.exec.update({"mode": "live", "equity": 1000.0, "open_count": 1,
                    "positions": [{"symbol": SYM, "entry": 61000.0, "stop": 58000.0,
                                   "volume": 0.01, "leverage": 10, "margin": 24.0, "mode": "live"}],
                    "pending": [], "by_pair": _bp(SYM, 61000.0, 58000.0, 0.01),
                    "rails_ok": True, "rails_reason": "", "halt": False})
    st.view = 1
    field = ui.export_frame_text(st, conn, width=200, height=54)
    assert "exec LIVE" in field and "BTC" in field and "1 pos" in field
    st.view = 2
    book = ui.export_frame_text(st, conn, width=200, height=54)
    assert "BTC" in book and "61,000" in book and "live" in book   # entry + live state
    conn.close()


def test_field_sort_buys_before_watch_by_score():
    """v6 attention sort: BUYs lead (score desc), watch follows, idle last."""
    st = AppState()
    now = time.time()
    for sym, status, score in (("LTC/USD", "BUY", 5), ("SUI/USD", "BUY", 4),
                               ("ADA/USD", "WATCH", 3), ("XRP/USD", "---", 0)):
        ps = st.pair(sym)
        ps.confirmed = Card(status=status); ps.confirmed.score = score
        ps.last_tick = _tick(1.0); ps.last_tick_ts = now
    ordered = [s for s, _, _ in ui._attention_sorted(st, now)]
    assert ordered.index("LTC/USD") < ordered.index("SUI/USD")      # BUY score desc
    assert ordered.index("SUI/USD") < ordered.index("ADA/USD")      # BUY before watch
    assert ordered.index("ADA/USD") < ordered.index("XRP/USD")      # watch before idle


def test_field_hold_state_for_position_without_buy(tmp_path):
    """A held position whose thesis is no longer a BUY reads as HOLD (teal), not
    idle — the money is still on the table."""
    conn = store.connect(str(tmp_path / "t.db"))
    st = AppState()
    now = time.time()
    ps = st.pair(SYM)
    ps.confirmed = Card(status="WATCH")       # no longer a BUY
    ps.last_tick = _tick(63000.0); ps.last_tick_ts = now
    st.exec = dict(st.exec)
    st.exec.update({"mode": "live", "equity": 1000.0, "open_count": 1,
                    "positions": [{"symbol": SYM, "entry": 61000.0, "stop": 58000.0,
                                   "volume": 0.01, "leverage": 10, "margin": 24.0, "mode": "live"}],
                    "pending": [], "by_pair": _bp(SYM, 61000.0, 58000.0, 0.01),
                    "rails_ok": True, "halt": False})
    st.view = 1
    txt = ui.export_frame_text(st, conn, width=200, height=54)
    assert "HOLD" in txt
    conn.close()
