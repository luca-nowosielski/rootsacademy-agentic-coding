# Saving Streak

Savings-loyalty points engine for the Roots Academy agentic-coding module.
FastAPI + SQLite backend, React + Vite frontend.

Earning and redeeming points, expiry, deposit-lot tracking, demo login and funded
transfers already work. **The loyalty bonus is the feature to build** — it is the
exercise, not a regression. See `exercises.md`.

`project_starter/saving-streak-spec.md` is the *target* spec. It describes features
that do not exist yet; do not treat it as a description of the current code.

## Commands

Backend, from `project_starter/app/backend`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m uvicorn saving_streak.api:app --host 127.0.0.1 --port 8787
.venv/bin/python -m pytest          # 248 tests, ~5s
.venv/bin/ruff check .
```

Frontend, from `project_starter/app/frontend`:

```sh
npm ci
npm run dev                          # http://127.0.0.1:5273
npm run lint
npm run build
```

Browser smoke checks and the isolated-fixture setup are documented in
`project_starter/readme.md` — follow that file, don't improvise a harness.

## Architecture invariants

These are the rules worth breaking a build over. Most of the code cites the spec
clause it implements as `spec D<n>`; keep that convention when adding behaviour.

- **One seam (spec D42).** All domain work goes through the application service in
  `service.py`. `api.py` only translates HTTP into service calls. Routes must never
  touch ledgers, repositories or connections directly.
- **The domain never reads the wall clock.** A `Clock` is handed in (`clock.py`).
  No `datetime.now()` in domain code — it makes time-dependent rules untestable.
- **Two ledgers that touch at one point (spec D6).** The points ledger (`ledger.py`)
  and deposit lots (`deposits.py`) are separate, but one deposit writes to both.
  Wrap such work in `atomically(conn)` from `db.py`: both commit or neither.
  Nesting `atomically` means one transaction, not two.
- **Events are at-least-once.** Handlers read before they append and must be
  idempotent — the same event can arrive twice, on two workers, at once.
- **Oldest first, everywhere.** Points are spent and expired oldest-first; deposit
  lots are consumed oldest-first on withdrawal.
- **Each storage module owns its DDL** via its own `ensure_schema()`. Register new
  ones in `migrations.py`.
- **Europe/Brussels for every date calculation** (`settings.TIMEZONE`, spec D5).
  Anniversaries and expiry are calendar-based, not 365-day arithmetic.
- **Whole cents only.** Money is validated to whole cents; points are integers.

## Gotchas

- **Ports are fixed.** Backend 8787, Vite 5273 with `strictPort`. Vite proxies
  `/api` to the backend; point it elsewhere with `SAVING_STREAK_API`.
- **`/api/events/*` are not customer commands.** They represent events core banking
  has already accepted, kept for the exercises. Customer-initiated transfers go
  through `/api/demo/transfers`.
- **StrictMode aborts are normal.** React 18 double-invokes effects in dev and the
  app aborts superseded reads on purpose. `ERR_ABORTED` in the console is expected
  and must not be "fixed" or surfaced as an error to the user.
- **Keep the SQLite file on a real local disk.** It runs in WAL mode; on a network
  or virtualised filesystem it fails at startup with `sqlite3.OperationalError:
  disk I/O error`. Override the location with `SAVING_STREAK_DB`, which defaults to
  `app/backend/saving-streak.db`.
- **Never commit** `.venv/`, `node_modules/` or `*.db` — all gitignored. Build
  artifacts are platform-specific; a venv or `node_modules` created on another OS
  will not work here.
- **The frontend never touches the database.** Everything goes through `/api`.

## Layout

- `project_starter/app/backend/src/saving_streak/` — `service.py` (the seam),
  `api.py` (HTTP), `ledger.py` / `deposits.py` (the two ledgers), `claims.py`,
  `catalogue.py`, `vouchers.py`, `banking.py`, `demo.py`, `clock.py`, `db.py`,
  `migrations.py`, `settings.py`
- `project_starter/app/backend/tests/` — one file per behaviour
  (`test_earning_points.py`, `test_expiring_points.py`, …)
- `project_starter/app/frontend/src/` — `App.jsx`, `Login.jsx`, `api.js`
- `project_starter/docs/reviews/` — prior independent review reports
- `docs/uml/` — architecture comparison of two completed implementations
  (reference material for the exercises, not diagrams of this starter)

## Demo profiles

Passwordless login: `anke@example.com` (history, expired points, a voucher),
`bram@example.com` (points near expiry), `lina@example.com` (fresh account).
Each starts with €2,500 spending funds alongside its savings. Deposits and
withdrawals move money between spending and savings and always preserve the total.
