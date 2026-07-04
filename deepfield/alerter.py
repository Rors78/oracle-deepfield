"""Alert chain — confirmed BUY transitions only. SPEC §11, invariant 7, F10.

On a confirmed BUY past the F10 cooldown (REALERT_HOURS): ledger row (confirmed)
-> tiered local sound (paplay wav -> aplay -> bell), each tier guarded by
shutil.which AND artifact existence, NEVER trusting return code alone (the banned
termux-media-player silent-swallow class) -> notify-send if present -> Telegram via
stdlib urllib POST iff env vars set. --test-alert exercises the whole chain (kind=test).

TODO(M5 path / M7 full): cooldown check, sound tiers, notify, telegram, test.
"""
