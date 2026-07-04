# ORACLE DEEPFIELD

Always-on terminal monitor: weekly + daily 7-signal cycle-bottom detection over
15 Kraken USD spot pairs. Live WebSocket v2 layer + closed-candle structural
scoring + a marked provisional layer. **Signal-only — no order execution.**

- Full build spec: [`docs/SPEC.md`](docs/SPEC.md)
- Authoritative rulings (supersede spec prose): [`docs/RULINGS.md`](docs/RULINGS.md)
- Parked ideas: [`docs/LATER.md`](docs/LATER.md)

## Run (once built)

```
python -m deepfield            # live TUI
python -m deepfield --simple   # plaintext frame every SIMPLE_SECS
python -m deepfield --once     # single confirmed eval + one frame (cron/tests)
```

tmux runbook + full usage land at M7.

## Dev

```
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/pytest -v
```

Status: **M0 scaffold.** Milestones M0–M7 in `docs/SPEC.md §13`.
