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
work with the service and records the M2 verification evidence and the M4 platform-contract
review.

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
- Run **exactly one** uvicorn worker process per container (no `--workers`); scale by running
  more containers (replicas). Required by spec §2 rule 6, because the Prometheus metrics live in
  the memory of one process (M4 item B8).
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
| `GET` | `/ready` | Readiness: `SELECT 1 FROM test_results LIMIT 0` → `200 {"status":"ok"}` or `503 {"status":"unavailable"}` |
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
  - Effective `from >= to` → 422. If `from`, `to` or the effective window cannot be represented
    as UTC timestamps within the years 1–9999 (e.g. `from=0001-01-01T00:00:00+01:00`, or only
    `to=0001-01-01T00:00:00Z`, whose default `from` would lie before year 1) → 422.
- **No maximum window, no maximum bucket count** (spec §7.1; M4 item B15/D8). Grouped reports and
  timeseries return one item per group or bucket that contains data, and the database work is
  bounded by the 10-second statement timeout (see [Operational details](#operational-details)).
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
| `422` | `from`, `to` or the effective window not representable in UTC (years 1–9999) | `{"detail":"from/to out of supported range"}` |
| `503` | PostgreSQL cancelled the report query (SQLSTATE `57014`, see below) | `{"detail":"Database query timed out"}` |
| `503` | Database otherwise unreachable/unusable (SQLAlchemy `OperationalError`, `InterfaceError`, pool `TimeoutError`) | `{"detail":"Database unavailable"}` |
| `503` | `/ready` when the table check fails (any database error) | `{"status":"unavailable"}` |
| `500` | Any other unexpected server/database error, SQLAlchemy or not | `{"detail":"Internal server error"}` |

**Query cancelled (spec §7.3).** When PostgreSQL cancels a report query (SQLSTATE `57014`
`query_canceled`, e.g. because the 10-second statement timeout was exceeded; a cancellation by
other means, such as `pg_cancel_backend()`, gives the same answer), the API returns
`503 {"detail":"Database query timed out"}` and writes **exactly one WARNING line** that names the
SQLSTATE and the route, e.g.
`Report query cancelled (SQLSTATE 57014, e.g. statement_timeout) on GET /api/v1/reports/summary`.
That line contains no traceback, no exception text, no SQL text, no parameters and no connection
details.

Other database details (SQL, host, credentials, driver messages) are logged server-side only and
never returned. For an unexpected error that is not a SQLAlchemy error, the application logs one
line with the route and the exception class (not its text), and uvicorn logs the single traceback
(M4 item B9).

*Historical (M2, 2026-09-25):* authentication failures (e.g. a wrong database password) raise
`OperationalError` and are mapped by the application to HTTP `503`. During M2 Stage 8 this was
observed manually on `/api/v1/reports/summary` after an incorrectly entered `evp_reader` password
(see manual evidence, step 7).

### Operational details

- A new database connection gives up after **3 seconds** (`connect_timeout`); Kubernetes probe
  timeouts must be longer.
- Every application connection starts with `default_transaction_read_only=on`: an accidental
  write from application code is refused by PostgreSQL (SQLSTATE `25006`). This is defence in
  depth only; the real boundary is the SELECT-only role.
- Every application connection also sets `statement_timeout=10s` (spec §7; M4 item B14/D7). It
  is a fixed constant (`DB_STATEMENT_TIMEOUT` in `app/db.py`, sent in the same libpq `options`
  string as the read-only default), not configuration. Report queries share the database with
  result ingestion; a longer statement is cancelled by PostgreSQL (see
  [Errors](#errors)).
- `/ready` runs `SELECT 1 FROM test_results LIMIT 0` (spec §2 rule 5; M4 item B16/D9). It reads
  no rows, but PostgreSQL still checks that `test_results` exists and that the database user may
  `SELECT` from it, so a missing migration or a missing grant makes the service not ready. A
  failed check logs one short warning line (exception class only).
- uvicorn's per-request access lines use the same `text`/`json` format as the application.
  Access lines for exactly `/health`, `/ready` and `/metrics` (a query string is ignored) are
  **not** written when the status is below `400`; failures (e.g. `/ready` → `503`) are always
  written. `LOG_LEVEL` also applies to uvicorn's own loggers (spec §9).
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
- **M4 additions.** `test_application_connection_has_statement_timeout_and_read_only_default`
  checks both connection settings (`10s`, `on`) on the application's own engine;
  `test_application_recovers_after_a_real_query_cancellation` causes a real SQLSTATE `57014`
  (with a transaction-local 50 ms timeout) and then checks that a new session from the
  application's session factory still has `statement_timeout=10s`, runs ordinary SQL, and that
  `/ready` returns 200.
- **Run sequentially.** The Results API integration suite `TRUNCATE`s `test_results` in the same
  `evp_test` database. **Never run both suites at the same time.**

---

## Verification evidence (M2, historical)

This section records the M2 evidence (2026-09-25, M2 code). At that time `/ready` ran
`SELECT 1`; the M4 table check (`SELECT 1 FROM test_results LIMIT 0`) is therefore **not**
covered by manual steps 5–6 below, but by the [M4 verification](#m4-verification).

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

### B. Platform contract (spec §2)

The M2 platform-contract table is superseded by the
[Platform contract review (M4)](#platform-contract-review-m4) below; the M2 version remains in the
Git history.

---

## Platform contract review (M4)

Application implementation reviewed through commit `94cc3ae` against spec §2, on 2026-10-22.
**Status** describes the application side. Deployment items (images, Kubernetes, CI, monitoring)
are platform work; they are listed as follow-up and are not assessed here. Evidence types:
*automated* (test names), *MANUAL M4* (2026-10-22, see [M4 verification](#m4-verification)),
*historical MANUAL M2* (2026-09-25, see above).

| # | Rule (spec §2) | Status | Evidence | Platform follow-up |
|---|---|---|---|---|
| 1 | All configuration via env vars | MET | `app/config.py`; `tests/unit/test_config.py`; no hosts, ports or credentials in code. `DB_CONNECT_TIMEOUT_S = 3` is a specified constant (B11); `DB_STATEMENT_TIMEOUT = "10s"` is fixed by B14/D7 and spec §7 | ConfigMap for non-secret values, Secret for `DB_PASSWORD` |
| 2 | Fail fast on bad config | MET | Automated: `tests/unit/test_config.py` (the message names the variable, never the value; exit 1). There is no migration command in this service | – |
| 3 | Stateless | MET | No local files; all data in PostgreSQL; nothing is written | – |
| 4 | Logs to stdout, secrets never logged, format selectable | MET | `app/logging_config.py`; automated: `tests/unit/test_logging_config.py` (incl. `test_logging_settings_and_db_url_never_shows_the_password`, `test_log_level_applies_to_uvicorn_loggers`, `test_successful_probe_access_lines_are_suppressed`). MANUAL M4: no access lines for successful `/health` and `/ready` | Log collection from stdout |
| 5 | `/health` (no DB), `/ready` (`SELECT 1 FROM test_results LIMIT 0`) | MET | `app/routers/health.py` (B16/D9); automated: `test_ready_returns_ok_when_the_table_check_succeeds`, `test_ready_returns_503_for_any_database_error`, integration `test_ready_returns_ok_with_real_database`. MANUAL M4: `/ready` → HTTP 200 as the SELECT-only `evp_reader`. Historical MANUAL M2 step 6: 503 while PostgreSQL was stopped, recovery without restart (with the M2 `SELECT 1` check) | Liveness/readiness probes; probe `timeoutSeconds` longer than the 3 s connect timeout |
| 6 | `/metrics`; exactly one uvicorn worker per container | MET | `app/metrics.py`; automated: `test_metrics_endpoint_uses_prometheus_format`, `test_ordinary_route_produces_standard_http_metrics`, `test_no_reporting_specific_custom_metrics`. One worker per container is a spec requirement (B8, see [Running locally](#running-locally-with-the-shared-root-env)) | Prometheus scrape config; run command without `--workers`, scale with replicas |
| 7 | Migrations are a separate command, never on startup | MET | This service has no migrations and no Alembic dependency (it never changes the schema, spec §7); `create_app()` does not touch the schema (`test_importing_main_has_no_side_effects`) | Results API migration Job must run before this service is rolled out |
| 8 | Graceful shutdown on SIGTERM | MET | uvicorn default; lifespan disposes the engine (`test_lifespan_creates_engine_and_session_factory_and_disposes_engine`). MANUAL M4: `kill -TERM` → full graceful shutdown sequence, no traceback (first runtime SIGTERM check of this service) | Termination grace period; draining of in-flight requests under load and Kubernetes termination not verified |
| 9 | Build once, deploy everywhere | MET | No environment-specific code paths; only env vars differ (`APP_ENV` affects logs only) | One image for dev and prod |
| 10 | Unique URL prefix `/api/v1/reports` | MET | Router prefix in `app/routers/reports.py`; `test_openapi_lists_exactly_the_expected_routes_and_no_security` | Ingress path routing |
| 11 | Swagger UI `/docs` enabled | MET | FastAPI default, no environment toggle; `test_swagger_ui_is_enabled`; historical MANUAL M2 step 7 (`/docs` → 200) | Verify `/docs` in the deployed environments |

**Read-only database access (spec §7, in addition to §2):** the security boundary is the
SELECT-only role (historical MANUAL M2 step 4: all writes rejected with `42501`; MANUAL M4: the
service ran as `evp_reader` with `/ready` and all checked reports HTTP 200). The application-level
defence in depth is `default_transaction_read_only=on` (integration test, `25006`).

## M4 verification

The application implementation was reviewed and tested through commit `94cc3ae`. These README
changes are documentation only.

### Automated (offline, run by Claude Code, 2026-10-22)

| Check | Result |
|---|---|
| `flake8` | no findings, exit 0 |
| `pytest -m "not integration"` | **394 passed, 19 deselected**, 1 warning (`StarletteDeprecationWarning`, B6), exit 0 |

### MANUAL (performed by Muhammad, 2026-10-22, local PostgreSQL)

| Check | Result |
|---|---|
| Integration suite `pytest -m integration -v` against `evp_test`, started only after the Results API suite had finished | **19 passed, 394 deselected**, 1 warning (B6), 0.74 s, exit 0; passing tests include `test_ready_returns_ok_with_real_database`, `test_application_connection_is_read_only_and_rejects_writes`, `test_application_connection_has_statement_timeout_and_read_only_default`, `test_application_recovers_after_a_real_query_cancellation` |
| API start as `evp_reader` (`PORT=8002`, password typed with `read -rs`, `LOG_LEVEL=INFO`) | `Started server process [3207]`; `/health` and `/ready` → `{"status":"ok"}`, HTTP 200; no access lines for these successful probe requests |
| Baseline before the simulator run | `/api/v1/reports/summary` → HTTP 200, total 0 (the newest existing rows were outside the default 7-day window) |
| Reading the 5 results stored by the end-to-end simulator run | `summary` → total 5, passed 5, failed 0, `pass_rate_percent` 100.0; `by-device` → 5 groups of 1/1/0; `timeseries?interval=hour` → bucket `2026-10-22T09:00:00Z` 5/5/0; all HTTP 200 |
| SIGTERM (`kill -TERM 3207`, the PID printed for the uvicorn process) | `Shutting down` → `Waiting for application shutdown.` → `Reporting API stopped` → `Application shutdown complete.` → `Finished server process [3207]`; no traceback; shell exit status 143 (128 + SIGTERM); afterwards `ps -p 3207` → no process |

---

## Known limitations and M4 traceability

M4 findings are tracked in GitHub issue #3. IDs: `B#` = backlog item from issue #3, `N#` = new
finding of the M4 audit, `D#` = M4 decision.

### Resolved in M4 (Reporting API)

| ID | Finding | Resolution | Commit |
|---|---|---|---|
| B14 (D7) | No `statement_timeout` for report queries | Fixed `statement_timeout=10s` per connection; SQLSTATE `57014` → `503 {"detail":"Database query timed out"}` with one safe WARNING line | `a5f9fd6` |
| B16 (D9) | `/ready` ran only `SELECT 1` and missed a missing table or grant | `/ready` runs `SELECT 1 FROM test_results LIMIT 0` | `a5f9fd6` |
| N3 | Out-of-range `from`/`to` gave a plain-text 500 | 422 `{"detail":"from/to out of supported range"}` | `a5f9fd6` |
| B9 | Non-SQLAlchemy exceptions gave a plain-text 500 | JSON `{"detail":"Internal server error"}` for every unexpected error | `4520d3c` |
| B7 (D5) | Successful probe/scrape requests filled the access log | Access lines for `/health`, `/ready`, `/metrics` with status < 400 are not written | `4520d3c` |
| N4 | uvicorn's `color_message` leaked into JSON log lines | Omitted from JSON output | `4520d3c` |
| N10 | `LOG_LEVEL` did not apply to uvicorn's loggers | uvicorn loggers follow `LOG_LEVEL` | `4520d3c` |

Related change in the Results API (not a Reporting finding): the earlier difference in
`from >= to` handling is gone. The Results API now also returns
`422 {"detail":"from must be earlier than to"}` (B20, fixed in the Results API, `d601c0b`).

### Operational requirements and specified constraints

- **Exactly one uvicorn worker per container (B8).** Spec §2 rule 6: the metrics live in the
  memory of one process; scale with replicas, never with `--workers`. This is a requirement for
  the run command (platform), not an open defect.
- **Fixed timeouts.** The 3 s connect timeout (B11) and the 10 s statement timeout are code
  constants on purpose (spec §7, §8.3.2). B11 was raised as a question whether the connect timeout
  should be configurable; that was intentionally rejected (accepted, documented no-change
  decision).

### Accepted risks and trade-offs

- **No maximum report window or bucket count (B15/D8).** For example `interval=hour` over several
  years is allowed. Only groups/buckets that contain data are returned (no zero-fill), and the
  database work is bounded by the 10 s statement timeout (spec §7.1).
- **Deprecation warning (B6).** Starlette's TestClient warns that `httpx` is deprecated in favour
  of `httpx2`. It affects only the tests (dev dependency); revisit during dependency maintenance.
- **Duplicated infrastructure code (B18).** Config, logging, database setup, health and metrics
  are intentionally copied from the Results API, so each service stays self-contained.
- **Configuration errors are plain text (N7).** The fail-fast message is printed before the log
  format is known, so it is plain text even with `LOG_FORMAT=json`.

### Deferred platform / CI work (not implemented)

- **Automated `evp_reader` check in CI (B17).** The SELECT-only role and its privileges were
  verified manually (M2 steps 1–4; M4 run as `evp_reader`). Provisioning the role and checking its
  privileges automatically in CI is future CI/CD work.
- **CI ordering (B19).** CI must apply the Results API migrations, then run the Results suite,
  then this suite, sequentially (both use `evp_test`; never in parallel).
- **TCP keepalive (B10).** If a pooled connection's network path dies silently (e.g. NAT or a load
  balancer in front of a managed database), `pool_pre_ping` may wait for the operating-system TCP
  timeout. libpq keepalive / `tcp_user_timeout` settings are future Terraform/cloud work.
- **Deployment items** from the platform-contract table: ConfigMap/Secret, probes, Prometheus
  scraping, single-worker run command, Ingress, image build, provisioning of `evp_reader`.
