"""M7 regression tests: legacy dca_log.csv import (F10 seeding) + CSV export."""
import datetime

from deepfield import store, alerter, engine, config


def _write_csv(path, rows):
    import csv
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "symbol", "price", "score", "signals"])
        w.writerows(rows)


def test_import_legacy_maps_display_symbol_and_localizes_denver_to_utc(tmp_path):
    """Regression: naive legacy timestamps are LOCAL (America/Denver) wall-clock,
    not UTC — importing them as UTC directly would skew the F10 cooldown by the
    local offset (6-7h). 04:23 MDT (summer, UTC-6) must land at 10:23 UTC."""
    csv_path = tmp_path / "dca_log.csv"
    _write_csv(csv_path, [["2026-07-04 04:23", "LTC", "44.81", "5", "Below W-EMA200|W-Vol Accumulation"]])
    conn = store.connect(str(tmp_path / "t.db"))

    result = alerter.import_legacy_csv(conn, str(csv_path))
    assert result == {"imported": 1, "skipped": 0}

    ts = store.last_alert_ts(conn, "LTC/USD", "confirmed")
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    assert dt == datetime.datetime(2026, 7, 4, 10, 23, tzinfo=datetime.timezone.utc)
    conn.close()


def test_import_legacy_skips_unmapped_symbols(tmp_path):
    csv_path = tmp_path / "dca_log.csv"
    _write_csv(csv_path, [["2026-07-04 04:23", "NOTAREALCOIN", "1.0", "5", "x"]])
    conn = store.connect(str(tmp_path / "t.db"))
    result = alerter.import_legacy_csv(conn, str(csv_path))
    assert result == {"imported": 0, "skipped": 1}
    conn.close()


def test_imported_row_gates_f10_cooldown(tmp_path):
    csv_path = tmp_path / "dca_log.csv"
    _write_csv(csv_path, [["2026-07-04 04:23", "LTC", "44.81", "5", "x"]])
    conn = store.connect(str(tmp_path / "t.db"))
    alerter.import_legacy_csv(conn, str(csv_path))

    now = datetime.datetime(2026, 7, 4, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
    last_ts = store.last_alert_ts(conn, "LTC/USD", "confirmed")
    assert engine.should_alert(last_ts, now, config.REALERT_HOURS) is False  # ~1h40m later, still cooling down
    conn.close()


def test_export_alerts_csv_roundtrip(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    alerter.fire(conn, "BTC/USD", 60000.0, 6, 7, ["a", "b"], kind="confirmed")
    out_path = tmp_path / "out.csv"
    n = alerter.export_alerts_csv(conn, str(out_path))
    assert n == 1
    text = out_path.read_text()
    assert "BTC/USD" in text and "confirmed" in text
    conn.close()
