# Reporting API (`reporting-api`)

The Reporting API is the **read-only analytics service** of the Engineering Validation Platform.
It answers questions such as "how many tests failed per device last week?" by aggregating the
`test_results` table in PostgreSQL.

- It **reads** results that the Results API stored. It does **not** insert, update or delete
  results, and it does **not** decide PASS/FAIL (the test bench does).
- It does **not** own the database schema and has **no** migrations.
- It is designed to run with a PostgreSQL user that may only `SELECT` (`evp_reader`).

The single source of truth for all technical requirements is
[`docs/APP_SPEC.md`](../../docs/APP_SPEC.md) (§7 for this service). This README explains how to
work with the service and records the M2 verification evidence.

```
Results API ──read/write (evp_writer)──▶ PostgreSQL ◀──SELECT only (evp_reader)── Reporting API
 owns schema + Alembic migrations          test_results                     aggregates in SQL
                                                                            /health /ready /metrics
```

---

## Architecture and ownership

| | Results API | Reporting API |
|---|---|---|
| Role | Ingestion (write) | Analytics (read) |
| Schema of `test_results` | **Owner**, Alembic migrations | Uses it; minimal SQLAlchemy Core description of 4 columns (`app/models.py`) |
| Migrations | `alembic upgrade head` (separate command) | None |
| Database user | `evp_writer` (read/write) | `evp_reader` (SELECT only) |
| Default port | 8001 | 8002 |

The Reporting API is an independent FastAPI service (own dependencies; designed to be built as its own image). Small
infrastructure modules (config, logging, database setup, health) are deliberately copied from the
Results API instead of shared, so each service stays self-contained.

## Requirements

- **Python 3.12**
- **PostgreSQL 16** (or compatible) with the Results-owned schema **already migrated**
  (run `alembic upgrade head` in `services/results-api`; see its README)
- A SELECT-only database user for normal operation (provisioning is platform work, see
  [Database access](#database-access-least-privilege))

---

## Local setup

```bash
cd services/reporting-api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt      # runtime deps (requirements.txt) + test/lint tools
```

All versions are pinned exactly. There is no `alembic` dependency.

## Configuration

All configuration comes from **environment variables**. The application reads process environment
variables. For local development, the shell sources `../../.env` before starting the service; the
Reporting API itself does not load that file. The complete contract is in
[`docs/APP_SPEC.md` §9](../../docs/APP_SPEC.md#9-configuration--logging).

| Variable | Required | Secret | Purpose |
|---|---|---|---|
| `APP_ENV` | no (default `dev`) | no | `dev` or `prod`; appears in logs |
| `LOG_LEVEL` | no (default `INFO`) | no | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `LOG_FORMAT` | no (default `text`) | no | `text` (local) or `json` (one JSON object per line, `"service":"reporting-api"`) |
| `PORT` | yes | no | Validated at startup; passed to uvicorn as `--port "$PORT"` |
| `DB_HOST`, `DB_PORT`, `DB_NAME` | yes | no | PostgreSQL connection |
| `DB_USER` | yes | no | Should be a **SELECT-only** role such as `evp_reader` |
| `DB_PASSWORD` | yes | **yes** | Password of `DB_USER` |
| `TEST_DB_HOST`, `TEST_DB_PORT`, `TEST_DB_NAME`, `TEST_DB_USER`, `TEST_DB_PASSWORD` | integration tests only | password: **yes** | Separate test database |

The Reporting API has **no API key** (it never writes). If a required variable is missing or
invalid, the service prints one line per problem (variable name and reason, never the value) and
exits with code 1 before the server starts.

### Running locally with the shared root `.env`

The root `.env` (git-ignored, created from `.env.example`) contains the Results API values
(`PORT=8001`, `DB_USER=evp_writer`). Load it **first**, then override the Reporting values in the
same shell. Do not edit `.env` for this, and never write the reader password into a file:

```bash
cd services/reporting-api
source .venv/bin/activate
set -a; source ../../.env; set +a        # 1. shared values (host, port, database, logging)

export PORT=8002                          # 2. Reporting overrides
export DB_USER=evp_reader
read -s DB_PASSWORD; export DB_PASSWORD; echo    # typed without echo, not in shell history

python -m uvicorn --factory app.main:create_app --host 127.0.0.1 --port "$PORT"
```

- The app is built by a factory, so importing the code has no side effects.
- `--host 127.0.0.1` listens on loopback only. Inside a container, `--host 0.0.0.0` will be
  needed (containerisation is platform work).
- Run **one** worker (no `--workers`), see [Known limitations](#known-limitations--m4-backlog).
- Swagger UI: <http://127.0.0.1:8002/docs>.

---

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/reports/summary` | `{"from","to","total","passed","failed","pass_rate_percent"}` |
| `GET` | `/api/v1/reports/by-device` | `{"from","to","items":[{device_id,total,passed,failed,pass_rate_percent}]}` |
| `GET` | `/api/v1/reports/by-test` | `{"from","to","items":[{test_name,total,passed,failed,pass_rate_percent}]}` |
| `GET` | `/api/v1/reports/timeseries?interval=hour\|day` | `{"from","to","items":[{bucket_start,total,passed,failed}]}` |
| `GET` | `/health` | Liveness: process is running, **no database access** → `200 {"status":"ok"}` |
| `GET` | `/ready` | Readiness: `SELECT 1` → `200 {"status":"ok"}` or `503 {"status":"unavailable"}` |
| `GET` | `/metrics` | Prometheus text format |
| `GET` | `/docs` | Swagger UI |

No endpoint needs authentication. Only `GET` is allowed on the report paths (others → `405`).

### Report semantics (spec §7)

- **Common query parameters:** `from`, `to` (timezone-aware ISO 8601), `device_id`,
  `test_name` (optional, exact match, no format check; an unknown value gives an empty result).
- **Window:** `from` inclusive, `to` exclusive (`from <= started_at < to`). Defaults: rolling
  window of exactly 7 × 24 hours, not aligned to midnight.
  - neither → `[now − 7 days, now)`
  - only `from` → `[from, now)`
  - only `to` → `[to − 7 days, to)`
- **UTC:** the effective `from`/`to` and `bucket_start` are always returned in UTC (`...Z`), also
  if the client sent another offset. In URLs, encode `+` as `%2B` (e.g. `+02:00` → `%2B02:00`);
  `curl --data-urlencode` or a client's `params=` does this automatically.
- **Pass rate:** `passed / total × 100`, rounded to one decimal **half-up** (exact decimal
  arithmetic: 1/16 = 6.25 % → `6.3`). `total == 0` → `null` (only possible in `summary`).
- **Ordering:** `by-device` / `by-test` sort by `failed` descending, then by the key ascending.
- **Timeseries:** `interval` is **required** and must be `hour` or `day`. Buckets are cut in UTC
  (`date_trunc(<interval>, started_at, 'UTC')`), independent of the database session time zone.
  Only buckets that contain results are returned (no zero-filled buckets); the first bucket may
  start before `from`.
- **Aggregation happens in PostgreSQL** (`COUNT`, `FILTER`, `GROUP BY`, `date_trunc`); Python
  receives one row per summary, group or bucket and only computes the pass rate.

### Errors

| Status | When | Body |
|---|---|---|
| `422` | Naive or malformed `from`/`to`, missing/invalid `interval` | FastAPI's standard validation body `{"detail":[...]}` |
| `422` | Effective `from >= to` | `{"detail":"from must be earlier than to"}` |
| `503` | Database unreachable/unusable (SQLAlchemy `OperationalError`, `InterfaceError`, pool `TimeoutError`) | `{"detail":"Database unavailable"}` |
| `500` | Any other SQLAlchemy error | `{"detail":"Internal server error"}` |

Database details (SQL, host, credentials, driver messages) are logged server-side only.
Authentication failures (e.g. a wrong database password) raise `OperationalError` and are mapped
by the application to HTTP `503`. During Stage 8 this was observed manually on
`/api/v1/reports/summary` after an incorrectly entered `evp_reader` password (see manual evidence,
step 7). `/ready` returns `503 {"status":"unavailable"}` for any database error on `SELECT 1`
(covered by unit tests; not observed during that incident).

### Operational details

- A new database connection gives up after **3 seconds** (`connect_timeout`); Kubernetes probe
  timeouts must be longer.
- Every application connection starts with `default_transaction_read_only=on`: an accidental
  write from application code is refused by PostgreSQL (SQLSTATE `25006`). This is defence in
  depth only; the real boundary is the SELECT-only role.
- `/metrics` contains only standard HTTP metrics (`http_requests_total{handler,method,status}`,
  latency histograms), labelled by route template. `/health`, `/ready`, `/metrics` are excluded.
  There are no custom `evp_*` metrics in this service.

---

## Database access (least privilege)

The Reporting API needs exactly:

| Object | Privilege |
|---|---|
| database | `CONNECT` |
| schema `public` | `USAGE` |
| table `public.test_results` | `SELECT` |

It needs **no** `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER`, and no
`CREATE` on schema `public`, no role membership and no administrative role attributes.

Creating the role, setting its password and granting privileges is **platform/security work**
(not application code, migrations or tests). It was done manually for M2; see the manual evidence
below.

**`TEMPORARY` on the database:** observed as `true` for `evp_reader` (PostgreSQL grants it to
`PUBLIC` by default). It only allows the role to create its own temporary tables, which disappear
at session end. It gives no write access to `public.test_results`. It was intentionally not
revoked globally, because that would affect every role in the database.

---

## Tests

```bash
cd services/reporting-api
source .venv/bin/activate
flake8
pytest                       # unit tests only
pytest -m integration -v     # PostgreSQL integration tests
```

### Unit tests: `pytest`

Plain `pytest` runs only non-integration tests (`setup.cfg`: `addopts = -m "not integration"`).
They need **no database, no network and no environment variables**; an autouse fixture removes
all settings variables from the environment, so a loaded `.env` cannot change results. The
database is replaced by a read-only fake session. These tests prove configuration, logging,
routing, validation, the SQL that is built (compiled for PostgreSQL), error mapping and metrics,
not PostgreSQL behaviour.

### Integration tests: `pytest -m integration -v`

Runs **only** the integration suite against **real PostgreSQL** (no SQLite):

```bash
cd services/reporting-api
source .venv/bin/activate
set -a; source ../../.env; set +a
pytest -m integration -v
```

- **`TEST_DB_*` only.** All five variables are required. There is no fallback to `DB_*`.
  Missing values are a test **error**, never a skip.
- **Safety guard.** `TEST_DB_NAME` must end with `_test` and differ from `DB_NAME`. Before any
  write, the suite checks `SELECT current_database()` against `TEST_DB_NAME`. The integration
  target (host, port, name, user; never the password) is printed at the start of the run.
- **Schema precondition.** The Results-owned `public.test_results` must already exist with the
  expected column types (`text`, `text`, `text`, `timestamp with time zone`). The suite never
  creates, migrates or alters the schema; if it is missing, the tests fail with a hint to apply
  the Results API migrations first.
- **Isolation.** Fixture rows carry a per-run `source = 'reporting-it-<uuid>'` and lie in a fixed
  historical period (June 2001). Before seeding, that period must contain no rows at all.
  Otherwise the suite stops and prints a manual cleanup statement that it does not execute.
- **Cleanup.** After the run, exactly the rows with the current marker are deleted and a count
  confirms 0. A hard kill (e.g. Ctrl+C during setup) can leave rows behind; the precondition
  above then reports them.
- **Real application.** Tests use `create_app()`, the FastAPI lifespan and the application's own
  engine (with its read-only default). A separate test-only connection seeds and removes rows.
- **Run sequentially.** The Results API integration suite `TRUNCATE`s `test_results` in the same
  `evp_test` database. **Never run both suites at the same time.**

---

## Verification evidence (M2)

### Automated evidence

**Default suite** (after the README was written, 2026-09-25): `flake8` clean,
`pytest` → **303 passed, 17 deselected**, 1 warning (`StarletteDeprecationWarning`, see
limitations).

**Stage 7: PostgreSQL integration suite** (run by Muhammad, 2026-09-25, commit `2363a94`):

| Item | Result |
|---|---|
| Target | `TEST_DB_HOST=localhost TEST_DB_PORT=5432 TEST_DB_NAME=evp_test TEST_DB_USER=evp_writer` |
| Result | **17 passed**, 303 deselected, **0 skipped**, 1 known warning; run successfully **twice** |
| Leftover fixture rows | `SELECT count(*) FROM test_results WHERE source LIKE 'reporting-it-%'` → **0** |

What these 17 tests prove against real PostgreSQL:

- **Safety and schema:** `TEST_DB_*` guard, `current_database()` check before writes, and the
  Results-owned column types match the Reporting model.
- **Reports:** exact `summary` counts and pass rate; exact `device_id` / `test_name` filters;
  no-match → zeros and `null` pass rate; exact `by-device` / `by-test` aggregation and order,
  including the `failed DESC, key ASC` tie-break.
- **Window:** `[from, to)`, i.e. a row exactly at `from` is included and a row exactly at `to` is
  excluded.
- **Timeseries:** hourly and daily buckets, executing `date_trunc … GROUP BY bucket_start` in
  PostgreSQL.
- **UTC days:** with the PostgreSQL session verified as `Europe/Berlin` (`SHOW timezone`), a row
  at `2001-06-20T23:30Z` still lands in the UTC day bucket `2001-06-20T00:00:00Z`.
- **Application read-only default:** `/ready` returns 200 with a real database. On the
  application's own engine, `SHOW default_transaction_read_only` → `on`, and a valid `INSERT` is
  rejected with SQLSTATE **`25006`** (`read_only_sql_transaction`); the writer connection confirms
  **0** rows stored.

The `25006` result proves the **application-level** defence only. It does not prove database
authorization; that is the manual evidence below.

### Manual evidence

Performed by Muhammad on **2026-09-25** against the local development database
(`DB_HOST=localhost`, `DB_PORT=5432`, `DB_NAME=evp`). Role provisioning was done manually as
platform work.

**1. `evp_reader` role attributes and memberships**

`rolsuper`, `rolcreatedb`, `rolcreaterole`, `rolreplication`, `rolbypassrls` all `false`.
Memberships: **none** (no writer or admin role).

**2. Effective privileges** (PostgreSQL `has_*_privilege` functions)

| Object | Privilege | Result |
|---|---|---|
| database `evp` | `CONNECT` | true |
| database `evp` | `TEMPORARY` | true (PostgreSQL default via `PUBLIC`, see above) |
| schema `public` | `USAGE` | true |
| schema `public` | `CREATE` | **false** |
| `public.test_results` | `SELECT` | true |
| `public.test_results` | `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER` | **all false** |

**3. Direct login as `evp_reader` (psql)**

`current_user` = `evp_reader`, `current_database` = `evp`,
`default_transaction_read_only` = **`off`**, and `SELECT count(*) FROM public.test_results`
succeeded (3 rows at that time).

**4. Write attempts rejected by role authorization**

Each statement ran in its **own** `BEGIN READ WRITE` transaction with verbose errors, followed by
`ROLLBACK`:

| Statement | Result |
|---|---|
| `INSERT` (fully valid row, `source='stage8-reader-permission-probe'`) | SQLSTATE **42501**, permission denied for table test_results |
| `UPDATE` | SQLSTATE **42501** |
| `DELETE` | SQLSTATE **42501** |
| `TRUNCATE` | SQLSTATE **42501** |

Afterwards, `SELECT count(*) … WHERE source = 'stage8-reader-permission-probe'` → **0**.

Because the transaction was explicitly `READ WRITE` and `default_transaction_read_only` was
`off`, these rejections come from the **role's missing privileges** (`42501`), not from the
application's read-only default (`25006`). Separate transactions were needed because after one
error PostgreSQL rejects every further statement in the same transaction with `25P02` without
checking permissions.

**5. Reporting API running as `evp_reader`** (`PORT=8002`, `DB_USER=evp_reader`, password entered
privately in the shell)

| Check | Result |
|---|---|
| `GET /health` | 200 `{"status":"ok"}` |
| `GET /ready` | 200 `{"status":"ok"}` |
| `GET /api/v1/reports/summary` | 200, `total=3, passed=3, failed=0, pass_rate_percent=100.0` |
| `GET /api/v1/reports/by-device` | 200, `ECU-001` 1/1/0 100.0; `ECU-003` 2/2/0 100.0 |
| `GET /api/v1/reports/by-test` | 200, `Sleep Current` 3/3/0 100.0 |
| `GET /api/v1/reports/timeseries?interval=day` | 200, bucket `2026-09-24T00:00:00Z` 3/3/0 |
| `pg_stat_activity` (queried with a separate account) | one backend with `usename=evp_reader`, `state=idle`, `client_addr=172.17.0.1` |

`pg_stat_activity` is PostgreSQL's list of open connections. It proves the application's pooled
connection is really authenticated as `evp_reader`. `idle` is expected: the pool keeps the
connection open after the request. `172.17.0.1` is the Docker bridge address under which the
host's connection appears inside the container; the application itself was configured with
`DB_HOST=localhost`.

**6. Database outage and recovery** (**one** uninterrupted Reporting API process, **not**
restarted during this sequence)

| Step | `/ready` | `/health` |
|---|---|---|
| PostgreSQL running | 200 | 200 |
| `evp-postgres` stopped | **503** `{"status":"unavailable"}` | **200** `{"status":"ok"}` |
| `evp-postgres` started, `pg_isready` → accepting connections | **200** `{"status":"ok"}` | 200 |

Readiness depends on PostgreSQL, liveness does not, and the running process reconnected
without a restart.

**7. Final checks (a separate, later start)**

After the outage test, the original process was stopped. For the last checks the API was
started again. On the first attempt the reader password was mistyped: `/metrics` and `/docs`
returned 200 while `/api/v1/reports/summary` returned **503**; a direct psycopg2 connection
reported `password authentication failed for user "evp_reader"`. After resetting the password
with `\password evp_reader` and re-entering it (direct check → `('evp_reader', 'evp')`), the
API was started again: `/metrics` → **200**, `/docs` → **200**, `/api/v1/reports/summary` →
**200**. This was a credential-entry mistake, not an application defect, and it happened after
the outage/recovery sequence above.

---

## M2 acceptance and traceability

### A. M2 criteria (spec §12)

| Requirement | Evidence | Status |
|---|---|---|
| All report endpoints | `app/routers/reports.py`: summary, by-device, by-test, timeseries; unit tests `tests/unit/test_reports_api.py`; Stage 7 integration tests; manual step 5 (all 200 as `evp_reader`) | MET |
| … with SQL aggregations | `app/queries.py` (`COUNT`, `FILTER`, `GROUP BY`, `date_trunc`); compiled-SQL unit tests `tests/unit/test_queries.py`; Stage 7 exact aggregates from real PostgreSQL | MET |
| `null` pass rate on empty data | `calculate_pass_rate_percent` (`tests/unit/test_calculations.py`: 0/0 → `None`); route unit test; Stage 7 `test_summary_without_matches_is_zero_with_null_pass_rate` | MET |
| Done when: works with a SELECT-only DB user | Manual steps 1–6: privilege matrix, `42501` for all four writes in `READ WRITE` transactions, API as `evp_reader` with all reports 200, `pg_stat_activity` identity | MET |
| Done when: integration tests cover summary | `test_summary_*`, `test_window_is_from_inclusive_to_exclusive` (Stage 7, 17/17 passed) | MET |
| Done when: integration tests cover by-device | `test_by_device_*` (Stage 7, 17/17 passed) | MET |

### B. Platform contract (spec §2), application side

Deployment-side items (ConfigMaps/Secrets, probes, Ingress, Prometheus scraping, images) are
platform work and **not started**. Application compatibility does not make them MET.

| Requirement | Evidence | Status |
|---|---|---|
| 1. Configuration via env vars | `app/config.py`; `tests/unit/test_config.py`; no hosts/ports/credentials in code | MET |
| 2. Fail fast on bad config | `load_settings()` exits 1 naming variables, never values; unit tests | MET |
| 3. Stateless | No local files; all data in PostgreSQL | MET |
| 4. Logs to stdout, secrets never logged, `text`/`json` | `app/logging_config.py`; `tests/unit/test_logging_config.py` (stdout only, password never in output) | MET |
| 5. `/health` (no DB), `/ready` (`SELECT 1`) | Unit tests; Stage 7 `/ready` 200; manual step 6 (503/200 during outage, recovery) | MET |
| 6. `/metrics` Prometheus format | `app/metrics.py`; unit tests; manual step 7 | MET |
| 7. No migrations on startup | No Alembic dependency or migrations; `create_app()` does not migrate; import has no side effects (unit test) | MET |
| 8. Graceful shutdown on SIGTERM | uvicorn default; lifespan disposes the engine (unit test) | PARTIAL (not verified under load or in Kubernetes) |
| 9. Build once, deploy everywhere | Platform work (image build), not started | N/A for M2 |
| 10. Unique prefix `/api/v1/reports` | Router prefix; OpenAPI path test | MET |
| 11. Swagger UI enabled | `/docs` unit test; manual step 7 | MET |
| Read-only DB access (spec §7) | Role authorization: manual step 4 (`42501`); application default: Stage 7 (`25006`) | MET |

---

## Known limitations / M4 backlog

These are deferred review items, not M2 failures. According to project history they are tracked
in the existing M4 backlog (GitHub issue #3); the issue's exact current content is not
reproduced here.

- **No `statement_timeout`** for report queries; a very large window keeps a connection busy.
- **No maximum report window or bucket count** (e.g. `interval=hour` over years).
- **`/ready` runs `SELECT 1` only.** It does not check the `SELECT` privilege on `test_results`
  or that the schema exists; a missing grant would show "ready" while reports fail.
- **The role check is manual.** Automated `evp_reader` provisioning and authorization checks
  in CI are deferred.
- **Duplicated infrastructure code** (config, logging, db, health) across the two APIs.
- **Shared `evp_test` database:** the Results and Reporting integration suites must run
  sequentially (CI ordering).
- **`from >= to` is handled differently:** Reporting returns 422, while Results API `GET /results`
  returns an empty list.
- **Deprecation warning:** Starlette's TestClient warns that `httpx` is deprecated in favour of
  `httpx2` (dev dependency only; tests pass).
- **Shared with the Results API:** single uvicorn worker (Prometheus metrics per process), probe
  requests still in the access log, non-SQLAlchemy exceptions not mapped to JSON, no TCP
  keepalive tuning for half-open connections.
