"""Which API keyfile is this process actually using?

Nothing answered that question until 2026-08-05, and the silence cost a day:
`~/.deepfield_keys` went missing, broker.load_keys quietly fell through to
`~/.hydra_keys` (a stale June key), Kraken answered `EAPI:Invalid key` and then
~650 `EGeneral:Temporary lockout`. The cascade was blamed on rate-limit pressure
for two whole boots because no line named the file in use.

Falling back is correct behaviour. Doing it mutely is not.

(Deliberately NOT in test_keys.py — that file is about KEYBOARD keys.)
"""
def _reset_key_cache(monkeypatch, keyfiles):
    from deepfield import broker
    monkeypatch.setattr(broker, "KEYFILES", keyfiles)
    monkeypatch.setattr(broker, "_KEY", None)
    monkeypatch.setattr(broker, "_SECRET", None)
    monkeypatch.setattr(broker, "_KEY_SRC", None)
    return broker


def test_primary_keyfile_is_named_at_info(tmp_path, monkeypatch, caplog):
    """Which key is this process actually using? Before 2026-08-05 nothing said."""
    from deepfield import broker
    primary = tmp_path / "primary"
    primary.write_text("KEY1\nSECRET1\n")
    b = _reset_key_cache(monkeypatch, [str(primary), str(tmp_path / "absent")])
    with caplog.at_level("INFO", logger="deepfield.broker"):
        k, s, src = b.load_keys()
    assert (k, s, src) == ("KEY1", "SECRET1", str(primary))
    assert any(str(primary) in r.getMessage() for r in caplog.records)


def test_fallback_keyfile_warns_loudly(tmp_path, monkeypatch, caplog):
    """The incident: ~/.deepfield_keys vanished, ~/.hydra_keys (stale, June) was used
    silently, Kraken answered EAPI:Invalid key and then locked the ACCOUNT out ~650
    times. The cascade was blamed on rate-limit pressure for two boots because no
    line named the file in use. Falling back is fine; doing it mutely is not."""
    from deepfield import broker
    missing = tmp_path / "gone"
    fallback = tmp_path / "stale"
    fallback.write_text("OLDKEY\nOLDSECRET\n")
    b = _reset_key_cache(monkeypatch, [str(missing), str(fallback)])
    with caplog.at_level("INFO", logger="deepfield.broker"):
        k, _, src = b.load_keys()
    assert (k, src) == ("OLDKEY", str(fallback))
    warned = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warned, "a silent fallback is the whole bug"
    msg = warned[0].getMessage()
    assert "FALLBACK" in msg and str(fallback) in msg and str(missing) in msg
    assert "lock" in msg.lower(), "name the consequence, not just the fact"


def test_no_keys_at_all_is_an_error_not_a_shrug(tmp_path, monkeypatch, caplog):
    from deepfield import broker
    b = _reset_key_cache(monkeypatch, [str(tmp_path / "a"), str(tmp_path / "b")])
    with caplog.at_level("INFO", logger="deepfield.broker"):
        assert b.load_keys() == (None, None, None)
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_short_keyfile_is_skipped_not_half_loaded(tmp_path, monkeypatch):
    """A truncated file (key, no secret) must fall through rather than authenticate
    with a None secret."""
    from deepfield import broker
    short = tmp_path / "short"
    short.write_text("ONLYKEY\n")
    good = tmp_path / "good"
    good.write_text("K\nS\n")
    b = _reset_key_cache(monkeypatch, [str(short), str(good)])
    assert b.load_keys() == ("K", "S", str(good))
