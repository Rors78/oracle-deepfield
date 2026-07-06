"""v6 key controls: the cbreak reader's exact/per-byte dispatch (incl. arrow
escape sequences) and the pure nav_* AppState mutators the handlers call.

Never opens a real TTY — the reader is driven through an os.pipe, so no terminal
state is touched (safe under pytest / CI)."""
import os

from deepfield import ui
from deepfield.keys import KeyController
from deepfield.state import AppState


def test_reader_exact_arrow_and_perbyte_dispatch():
    fired = []
    handlers = {
        b"j": lambda: fired.append("j"),
        b"k": lambda: fired.append("k"),
        b"1": lambda: fired.append("1"),
        b"\x1b[A": lambda: fired.append("up"),
        b"\x1b[B": lambda: fired.append("down"),
    }
    kc = KeyController(loop=None, handlers=handlers)
    r, w = os.pipe()
    kc._fd = r
    try:
        os.write(w, b"j"); kc._on_readable()
        assert fired[-1] == "j"                       # single key
        os.write(w, b"\x1b[A"); kc._on_readable()
        assert fired[-1] == "up"                      # arrow: exact multi-byte match
        os.write(w, b"\x1b[B"); kc._on_readable()
        assert fired[-1] == "down"
        os.write(w, b"1k"); kc._on_readable()
        assert fired[-2:] == ["1", "k"]               # burst → per-byte fallback
        before = len(fired)
        os.write(w, b"\x1b"); kc._on_readable()        # bare ESC: no handler, no crash
        assert len(fired) == before
    finally:
        os.close(r); os.close(w)


def _st_with_pairs():
    st = AppState()
    for sym in ui.PAIR_LIST:
        st.pair(sym)                                  # all idle → alpha order
    return st


def test_nav_select_moves_focus_clamped():
    st = _st_with_pairs()
    order = [s for s, _, _ in ui._attention_sorted(st, 0.0)]
    st.focus_symbol = order[0]
    ui.nav_select(st, 1)
    assert st.focus_symbol == order[1]
    ui.nav_select(st, -1)
    assert st.focus_symbol == order[0]
    ui.nav_select(st, -5)                              # clamp at top
    assert st.focus_symbol == order[0]
    st.focus_symbol = order[-1]
    ui.nav_select(st, 5)                               # clamp at bottom
    assert st.focus_symbol == order[-1]


def test_nav_expand_toggles_one_at_a_time():
    st = _st_with_pairs()
    st.focus_symbol = "BTC/USD"
    ui.nav_expand(st)
    assert st.expanded_symbol == "BTC/USD"
    st.focus_symbol = "ETH/USD"                        # move focus, expand another
    ui.nav_expand(st)
    assert st.expanded_symbol == "ETH/USD"             # only ONE expanded
    ui.nav_expand(st)                                  # toggle same → collapse
    assert st.expanded_symbol is None


def test_nav_view_cycle_preserves_focus_and_collapses(tmp_path):
    """Acceptance #4: 1→2→3→1 keeps focus, collapses expansion."""
    st = _st_with_pairs()
    st.focus_symbol = "SOL/USD"
    st.expanded_symbol = "SOL/USD"
    for n in (2, 3, 1):
        ui.nav_view(st, n)
        assert st.view == n
        assert st.focus_symbol == "SOL/USD"            # focus preserved
        assert st.expanded_symbol is None              # expansion collapsed


def test_nav_scroll_targets_active_view():
    st = _st_with_pairs()
    st.view = 1
    ui.nav_scroll(st, 1); assert st.ledger_scroll == 1
    ui.nav_scroll(st, -5); assert st.ledger_scroll == 0     # clamp >= 0
    st.view = 2
    ui.nav_scroll(st, 1); assert st.book_scroll == 1
    st.view = 3
    ui.nav_scroll(st, 2); assert st.journal_scroll == 2
