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


def test_plan_is_pure_and_matches_sizing(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    e = ex_mod.Executor(conn)
    plan = e.plan(SYM, 63000.0, Card(), 1000.0)
    assert plan["leverage"] == 10
    # risk 2% of 1000 = 20; stop = clamped support; vol = 20/(entry-stop)
    assert abs(plan["actual_risk"] - 20.0) < 0.5
    assert plan["stop"] < 63000.0 and plan["margin"] > 0
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
    assert "EXEC off" in ui.render_exec_line(st).plain
    st.exec = {"mode": "live", "equity": 1234.5, "open_count": 2, "positions": [],
               "rails_ok": True, "rails_reason": "", "halt": False, "updated": 0.0}
    line = ui.render_exec_line(st).plain
    assert "EXEC LIVE" in line and "$1,234.50" in line and "pos 2/" in line


def test_exec_line_shows_halt():
    st = AppState()
    st.exec = {"mode": "live", "equity": 100.0, "open_count": 0, "positions": [],
               "rails_ok": False, "rails_reason": "x", "halt": True, "updated": 0.0}
    assert "HALTED" in ui.render_exec_line(st).plain


def test_frame_renders_with_exec_and_positions(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    st = AppState()
    ps = st.pair(SYM)
    ps.confirmed = Card()
    ps.cooldown_until = time.time() + 3600     # gated
    ps.exec_plan = {"volume": 5.0, "leverage": 10, "margin": 24.0, "notional": 240.0,
                    "actual_risk": 20.0, "stop": 58000.0, "entry": 63000.0,
                    "floored_to_min": False, "capped": False}
    # position on a DIFFERENT symbol, so the gated champion shows COOLDOWN (a
    # position on the champion itself would correctly show POSITION OPEN instead).
    st.exec = {"mode": "paper", "equity": 1000.0, "open_count": 1,
               "positions": [{"symbol": "ETH/USD", "entry": 1800.0, "stop": 1600.0,
                              "volume": 0.5, "leverage": 10, "margin": 90.0, "mode": "paper"}],
               "rails_ok": True, "rails_reason": "", "halt": False, "updated": time.time()}
    txt = ui.export_frame_text(st, conn, width=110)
    assert "EXEC PAPER" in txt
    assert "ORDER" in txt and "@ 10x" in txt
    assert "COOLDOWN" in txt            # gated BUY (no position) reads as gated
    assert "POSITIONS (1)" in txt
    conn.close()


def test_champion_prefers_actionable_over_higher_score_gated():
    """Operator ruling: an actionable lower-score BUY beats a gated higher-score
    one for the champion card (so the card headlines what the bot will act on)."""
    st = AppState()
    now = time.time()
    tick = type("T", (), {"last": 1.0})()
    # LTC: higher score (5) but GATED (cooldown)
    ltc = st.pair("LTC/USD")
    ltc.confirmed = Card(status="BUY"); ltc.confirmed.score = 5
    ltc.cooldown_until = now + 3600
    ltc.last_tick = tick; ltc.last_tick_ts = now
    # SUI: lower score (4) but ACTIONABLE (fresh, no cooldown)
    sui = st.pair("SUI/USD")
    sui.confirmed = Card(status="BUY"); sui.confirmed.score = 4
    sui.cooldown_until = 0.0
    sui.last_tick = tick; sui.last_tick_ts = now
    picked = ui._pick_champion(st)
    assert picked is not None and picked[0] == "SUI/USD"   # actionable wins
    # sanity: if SUI were also gated, the higher-score LTC would win
    sui.cooldown_until = now + 3600
    assert ui._pick_champion(st)[0] == "LTC/USD"


def test_champion_shows_position_open_when_in_position(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    st = AppState()
    ps = st.pair(SYM)
    ps.confirmed = Card()
    ps.cooldown_until = time.time() + 3600
    ps.exec_plan = {"volume": 5.0, "leverage": 10, "margin": 24.0, "notional": 240.0,
                    "actual_risk": 20.0, "stop": 58000.0, "entry": 63000.0,
                    "floored_to_min": False, "capped": False}
    st.exec = {"mode": "live", "equity": 1000.0, "open_count": 1,
               "positions": [{"symbol": SYM, "entry": 63000.0, "stop": 58000.0,
                              "volume": 5.0, "leverage": 10, "margin": 24.0, "mode": "live"}],
               "rails_ok": True, "rails_reason": "", "halt": False, "updated": time.time()}
    txt = ui.export_frame_text(st, conn, width=110)
    assert "POSITION OPEN" in txt        # in-position takes precedence over cooldown
    conn.close()
