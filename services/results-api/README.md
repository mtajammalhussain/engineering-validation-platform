# Results API (`results-api`)

The Results API is the **ingestion service** of the Engineering Validation Platform. Test
benches (in this project: the simulator) send finished ECU test results to it; the API
validates each result, stores it in PostgreSQL and returns stored results on request.

- It **stores and returns** results. It does **not** judge them: the PASS/FAIL verdict is
  decided by the test bench and stored as sent.
- It **owns the database schema** (`test_results` table) through Alembic migrations.
- The **Reporting API** (read-only statistics) and the **Simulator** (test bench) are separate
  services with their own documentation. They are not covered here.

The single source of truth for all technical requirements is
[`docs/APP_SPEC.md`](../../docs/APP_SPEC.md). This README explains how to work with the service
and records the M1 verification evidence and the M4 platform-contract review.

```
Test bench / simulator ──HTTP POST /api/v1/results──▶ Results API ──read/write──▶ PostgreSQL
                                                         │
                                  /health /ready /metrics┘  (Kubernetes probes, Prometheus – planned)
```

---

## Local setup

Requirements: **Python 3.12**, Docker (only for the local PostgreSQL container below).

```bash
cd services/results-api
python3.12 -m venv .venv                 # isolated Python environment for this service
source .venv/bin/activate
pip install -r requirements-dev.txt      # runtime deps (requirements.txt) + test/lint tools
```

`requirements.txt` holds only what the running service needs; `requirements-dev.txt` includes it
and adds `pytest`, `pytest-cov`, `flake8` and `httpx`. All versions are pinned exactly.

### The `.env` file

The real `.env` file lives at the **repository root**, not inside `services/results-api/`. It is
git-ignored. Create it once from the template and adjust the dummy values:

```bash
cp ../../.env.example ../../.env          # run from services/results-api
```

The application reads **only environment variables**; it does not open `.env` itself. Load the
file into your shell before running the API, Alembic or the integration tests:

```bash
set -a; source ../../.env; set +a         # from services/results-api
```

(`set -a` exports every variable that `source` reads, so child processes such as `uvicorn` and
`alembic` can see them.)

## Configuration

Variables used by the Results API and its integration tests. The complete configuration contract
is in [`docs/APP_SPEC.md` §9](../../docs/APP_SPEC.md#9-configuration--logging).

| Variable | Required | Secret | Purpose |
|---|---|---|---|
| `APP_ENV` | no (default `dev`) | no | `dev` or `prod`; appears in logs |
| `LOG_LEVEL` | no (default `INFO`) | no | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `LOG_FORMAT` | no (default `text`) | no | `text` (local) or `json` (one JSON object per line) |
| `PORT` | yes | no | Validated at startup; passed to uvicorn as `--port "$PORT"` |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` | yes | no | PostgreSQL connection |
| `DB_PASSWORD` | yes | **yes** | PostgreSQL password |
| `RESULTS_API_KEY` | yes | **yes** | Expected value of the `X-API-Key` header for `POST` |
| `TEST_DB_HOST`, `TEST_DB_PORT`, `TEST_DB_NAME`, `TEST_DB_USER` | integration tests only | no | Separate test database |
| `TEST_DB_PASSWORD` | integration tests only | **yes** | Test database password |

If a required variable is missing or invalid, the service prints one line per problem (variable
name and reason, never the value) and exits with code 1 before the server starts.
Secrets are held as `SecretStr` and are never logged.

---

## Local PostgreSQL

> **Local development/test infrastructure only — not the project's final
> Docker/Compose/Kubernetes implementation.**

A single PostgreSQL 16 container, with credentials taken from the root `.env`:

```bash
cd <repository root>
set -a; source .env; set +a

docker run -d \
  --name evp-postgres \
  -e POSTGRES_USER="$DB_USER" \
  -e POSTGRES_PASSWORD="$DB_PASSWORD" \
  -e POSTGRES_DB="$DB_NAME" \
  -p "127.0.0.1:${DB_PORT}:5432" \
  postgres:16

docker exec evp-postgres pg_isready -U "$DB_USER" -d "$DB_NAME"   # "accepting connections"
```

- The port is bound to `127.0.0.1` only, so other machines cannot connect.
- There is no volume: `docker rm evp-postgres` deletes all data. `docker stop` / `docker start`
  keep it.
- `POSTGRES_USER` becomes a superuser. The least-privilege roles `evp_writer` / `evp_reader` of the
  final platform are provisioned by the platform layer, not here.

The integration tests need a **second, separate database** `evp_test` in the same container.
Create it once (safe to rerun: it only creates the database if it does not exist):

```bash
echo "SELECT 'CREATE DATABASE evp_test' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'evp_test')\gexec" \
  | docker exec -i evp-postgres psql -U "$DB_USER" -d postgres
```

## Database migrations

The Results API owns the schema of the `test_results` table. Schema changes are Alembic
migrations in `migrations/versions/`; the table definition itself lives only in
`app/models.py` (`Base.metadata`), which Alembic uses as its source.

```bash
cd services/results-api
set -a; source ../../.env; set +a
alembic upgrade head        # create/upgrade the schema
alembic current             # shows the applied revision, e.g. "1afbeabf0f46 (head)"
```

- Migrations are a **separate command**. They are **never** run automatically when the
  application starts.
- `alembic.ini` contains no database URL and no password. `migrations/env.py` builds the
  connection from the `DB_*` variables via `app/config.py` and `app/db.py`.
- `migrations/env.py` loads only the migration settings (`MigrationSettings`: `DB_*`, `APP_ENV`,
  `LOG_LEVEL`, `LOG_FORMAT`). The API-only variables `PORT` and `RESULTS_API_KEY` are **not**
  needed (spec §2 rule 7, §9; M4 item B1). Evidence: unit tests
  `test_env_py_loads_only_the_migration_settings` and
  `test_offline_migration_works_without_api_only_variables`; manual M4 run with both variables
  removed (see [M4 verification](#m4-verification)).

## Starting the API

The application is built by a factory function, so importing the code has no side effects:

```bash
cd services/results-api
set -a; source ../../.env; set +a
uvicorn --factory app.main:create_app --host 127.0.0.1 --port "$PORT"
```

- `--host 127.0.0.1`: listen on the local loopback only. Use this for local development on
  your machine.
- `--host 0.0.0.0`: listen on all network interfaces. This will be needed when the application
  runs **inside a container**, where `127.0.0.1` is only reachable from inside that container.
  Containerisation is part of the platform layer and is **not implemented yet**.
- Run **exactly one** uvicorn worker process per container (do not add `--workers`); scale by
  running more containers (replicas). Required by spec §2 rule 6, because the Prometheus metrics
  live in the memory of one process (M4 item B8).
- On `SIGTERM` / `Ctrl+C`, uvicorn shuts down and the application closes its database
  connection pool (log lines `Results API stopped`, `Application shutdown complete`).

Swagger UI: <http://127.0.0.1:8001/docs> (with `PORT=8001`). Use **Authorize** to enter the
API key for `POST` requests.

---

## API

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/v1/results` | `X-API-Key` | Store one result → `201` with the stored row (incl. `id`, `received_at`, `source`) |
| `GET` | `/api/v1/results` | none | List results, filters + pagination |
| `GET` | `/api/v1/results/{id}` | none | One result |
| `GET` | `/health` | none | Liveness: process is running, **no database access** |
| `GET` | `/ready` | none | Readiness: runs `SELECT 1` against PostgreSQL |
| `GET` | `/metrics` | none | Prometheus metrics (text format) |
| `GET` | `/docs` | none | Swagger UI |

**POST body** — validation rules (spec §5.2): `device_id` `ECU-` + 3 ASCII digits;
`test_name` 1–100 and `unit` 1–10 characters; `temperature_c` −40…125; `measured_value`,
`limit_min`, `limit_max` finite (limits optional); `verdict` exactly `PASS`/`FAIL`;
`duration_s` finite and > 0; `started_at` timezone-aware, representable as a UTC timestamp
(after conversion to UTC within the years 1–9999, e.g. `0001-01-01T00:00:00+01:00` is rejected)
and at most 5 minutes in the future; unknown fields rejected.
`source` may be omitted (PostgreSQL then stores `'simulator'`). If sent, it must be 1–100
characters and not only whitespace; it is stored exactly as sent (no trimming). `null` is
rejected. If **both** limits are given, `limit_min` must not be greater than `limit_max`
(`limit_min == limit_max`, one limit or no limit are valid). This only checks that the reported
limits are consistent with each other: there is no limit logic, the API never compares
`measured_value` with the limits, and the verdict is stored as sent.

**Storing a result** (spec §5.2) — the row is inserted and its database-generated values (`id`,
`received_at`, default `source`) are read back inside the same transaction; the commit is the
**last** database step (M4 items B2/N2). After a successful commit only in-memory work remains
(metrics, log line, response), so a stored result is no longer turned into a `5xx` by later
database work. **Residual risk (accepted, D1):** if the connection fails while the COMMIT itself is
being confirmed, the database may have stored the row although the client receives `503`; a client
that retries `5xx` (such as the simulator) can then store the result twice. Only request
idempotency, which is not part of this API, would remove this case.

**GET list** — query parameters `device_id`, `test_name`, `verdict`, `from`, `to`
(timezone-aware ISO 8601; `from <= started_at < to`), `limit` (1–500, default 50),
`offset` (default 0). If both `from` and `to` are given, `from` must be earlier than `to`;
`from >= to` → `422 {"detail":"from must be earlier than to"}`, the same rule and body as the
Reporting API (M4 item B20; a breaking change compared with M1, which returned an empty list).
Sorted by `started_at` descending, then `id` descending.
Response: `{"items": [...], "total": <all matching rows>, "limit": ..., "offset": ...}`.
In zsh, quote URLs that contain `?`.

**Authentication** — `POST` requires `X-API-Key` equal to `RESULTS_API_KEY`, compared in
constant time (`secrets.compare_digest`). The key is checked **before** the body is validated.
It is never logged or returned.

### Operational endpoints

- `/health` → always `200 {"status":"ok"}` while the process runs; it does not touch the database.
- `/ready` → `200 {"status":"ok"}` if PostgreSQL answers `SELECT 1`, otherwise
  `503 {"status":"unavailable"}`. A new database connection gives up after **3 seconds**
  (`DB_CONNECT_TIMEOUT_S` in `app/db.py`, passed to libpq as `connect_timeout`); this also applies to
  normal API requests. A future Kubernetes probe `timeoutSeconds` must be longer than this.
- `/metrics` → Prometheus text format:
  - `evp_results_received_total{test_name, verdict}` — incremented only after a successful commit
  - `evp_results_rejected_total{reason}` — `reason` is `auth` or `validation` (POST only)
  - standard HTTP metrics from `prometheus-fastapi-instrumentator`
    (e.g. `http_requests_total{handler, method, status}`, request duration histograms), labelled
    by route template, not raw URL. `/health`, `/ready` and `/metrics` are excluded from these.

**Logging of these endpoints** (spec §9) — uvicorn's per-request access lines use the same
`text`/`json` format as the application. Access lines for exactly `/health`, `/ready` and
`/metrics` (a query string is ignored; `/healthz` does not match) are **not** written when the
status is below `400`; failures (e.g. `/ready` → `503`) are always written, and an access record in
an unrecognised format is always kept. `LOG_LEVEL` also applies to uvicorn's own loggers (startup
and access lines).

### Error responses (as implemented)

| Status | When | Body |
|---|---|---|
| `401` | `POST` with missing or wrong `X-API-Key` | `{"detail":"Invalid or missing API key"}` |
| `404` | `GET /api/v1/results/{id}` with an unknown id | `{"detail":"Result not found"}` |
| `422` | Invalid POST body (incl. the `source`, limit-consistency and `started_at` rules above), invalid query parameters, invalid `{id}` (not an integer, < 1, above the PostgreSQL `bigint` maximum) | FastAPI's standard validation body: `{"detail":[...]}` |
| `422` | `GET /api/v1/results` with `from >= to` | `{"detail":"from must be earlier than to"}` |
| `503` | Database unreachable or unusable (SQLAlchemy `OperationalError`, `InterfaceError`, pool `TimeoutError`) on any `/api/v1/results` endpoint | `{"detail":"Database unavailable"}` |
| `503` | `/ready` when `SELECT 1` fails | `{"status":"unavailable"}` |
| `500` | Any other unexpected error, SQLAlchemy (e.g. `IntegrityError`) or not | `{"detail":"Internal server error"}` |

Failed writes are rolled back. Database details (SQL, host, credentials, driver messages) are
logged server-side only and never returned. For an unexpected error that is not a SQLAlchemy
error, the application logs one line with the route and the exception class (not its text), and
uvicorn logs the single traceback (M4 item B9).

---

## Tests

```bash
cd services/results-api
source .venv/bin/activate
flake8
pytest                       # unit tests only
pytest -m integration -v     # PostgreSQL integration tests
```

### Unit tests — `pytest`

Plain `pytest` runs **only non-integration tests** (`setup.cfg`: `addopts = -m "not integration"`).
They need **no database, no network and no environment variables**. The database is replaced
by a fake session; these tests prove validation, authentication, status codes, the SQL that is
built and the metrics logic — not PostgreSQL behaviour.

`pytest` currently prints one `StarletteDeprecationWarning` about `httpx`
(accepted, B6; see [Known limitations](#known-limitations-and-m4-traceability)).

### Integration tests — `pytest -m integration -v`

An explicit `-m integration` replaces the default marker expression and runs **only** the
integration suite, against **real PostgreSQL** (no SQLite). It requires all five `TEST_DB_*`
variables:

```bash
cd services/results-api
set -a; source ../../.env; set +a
pytest -m integration -v
```

- **Separate database.** Tests run against `evp_test`, never against the development database.
- **Safety guard.** Before any migration, `TRUNCATE` or direct SQL, the suite refuses to run
  unless `TEST_DB_NAME` ends with `_test` **and** differs from `DB_NAME`. Missing or empty
  `TEST_DB_*` variables are a test **error** (non-zero exit code), never a silent skip.
  Before each cleanup, the suite also asks PostgreSQL for `current_database()` and refuses if it
  is not the validated test database.
- **Schema via Alembic.** The schema of `evp_test` is created with the real
  `alembic upgrade head` (not `create_all()`); one test performs the round trip
  upgrade → downgrade → upgrade and always leaves the database at head.
- **Isolation.** Before each database test: `TRUNCATE test_results RESTART IDENTITY`, so every
  test starts with an empty table and ids starting at 1.
- **Real application.** Tests use `create_app()`, the FastAPI lifespan (`with TestClient(...)`),
  the real SQLAlchemy engine and sessions.
- Running a single integration file **without** `-m integration` deselects all its tests
  (pytest exit code 5, "no tests ran").

---

## Manual verification (M1, historical)

Manual checks complement the automated tests. The source of each check is stated explicitly.
These checks were performed during M1 (2026-09-24) against the M1 code; the M4 manual checks are
recorded separately under [M4 verification](#m4-verification).

### Step 4 — Alembic round trip (reported by Muhammad during the M1 implementation session)

- Performed by: Muhammad, manually, against the local `evp` database
- Date: 2026-09-24 (reported in the implementation session)
- Result: `alembic upgrade head` succeeded; `alembic downgrade base` removed the table;
  `alembic upgrade head` succeeded again; `alembic current` showed `1afbeabf0f46 (head)`;
  `\d test_results` in psql showed the expected columns, indexes and check constraint.

### Step 6 — API behaviour (manual, by Muhammad)

- Date: 2026-09-24, approximately 23:25–23:27 CEST
- Result:
  1. Valid `POST /api/v1/results` with correct `X-API-Key` → `HTTP 201 Created`; the stored
     result was returned with a generated `id` and `received_at`.
  2. Same valid POST without `X-API-Key` → `HTTP 401 Unauthorized`,
     `{"detail":"Invalid or missing API key"}`.
  3. POST with correct `X-API-Key` but `"verdict":"OK"` → `HTTP 422 Unprocessable Entity`;
     the validation error stated that `verdict` must be `"PASS"` or `"FAIL"`.

Only these three checks were performed manually. List and get-by-id, filtering, pagination,
wrong (as opposed to missing) API keys and similar behaviour are covered by the automated unit
and integration tests. Database outage and recovery are covered separately by step 7A.

### Step 7 — readiness

**A. Stopped-container test (manual, by Muhammad)**

PostgreSQL container stopped (`docker stop evp-postgres`) → `/ready` returned 503 quickly →
`/health` remained 200 → PostgreSQL restarted (`docker start evp-postgres`) → `/ready`
returned 200 again without restarting the API.

- Date: 2026-09-24, approximately 23:19 CEST
- Result: with the Results API running, `docker stop evp-postgres` → `GET /ready` returned
  `HTTP 503 {"status":"unavailable"}`; `GET /health` returned `HTTP 200 {"status":"ok"}`;
  `docker start evp-postgres` → without restarting the API, `GET /ready` returned
  `HTTP 200 {"status":"ok"}`.

**B. Unreachable-host test (performed by Claude Code during implementation)**

The API was started with `DB_HOST=10.255.255.1` (an address that silently drops packets).
`/ready` returned `503 {"status":"unavailable"}` after approximately **3.01 s**, and `/health`
still returned 200. This exercised the configured 3-second `connect_timeout`.
This is **not** the stopped-container test above: a stopped container usually refuses
connections immediately.

- Date: 2026-09-24

### Step 8 — destructive-test safety guard (performed by Claude Code)

With the development database `evp` present in the same container, the integration suite was
started with unsafe configurations. Each run failed with a clear message and exit code 1 before
touching PostgreSQL; `evp` was checked before and after (revision `1afbeabf0f46`, 2 rows,
max id 2 — unchanged).

| Configuration | Result |
|---|---|
| `TEST_DB_NAME=evp` | `Refusing to run: TEST_DB_NAME='evp' does not end with '_test'.` — 32 errors, exit 1 |
| `DB_NAME=evp_test` (equal to `TEST_DB_NAME`) | `Refusing to run: TEST_DB_NAME='evp_test' is the same as DB_NAME.` — 32 errors, exit 1 |
| `TEST_DB_HOST` unset | `Integration tests need a real PostgreSQL database. Missing or empty: TEST_DB_HOST` — 32 errors, exit 1 |

- Date: 2026-09-24 (first during step 8, repeated during the step 9 verification run)

---

## M1 acceptance and traceability

### Verification run

| Item | Value |
|---|---|
| Date | 2026-09-24, 22:58 CEST |
| Branch | `docs/results-api-m1` |
| Code | commit `9e5d494` (merge of PR #2, M1 code in `dev`); only this README was uncommitted |
| Python | 3.12.14 |
| `flake8` | no findings, exit 0 |
| `pytest` | **277 passed, 32 deselected**, 1 warning (`StarletteDeprecationWarning`), exit 0 — also with a completely empty environment |
| `pytest -m integration -v` | **32 passed, 277 deselected**, 1 warning (same), exit 0, 4.86 s, against `evp_test` |
| Fail fast | real app without configuration: 7 missing variables listed, `uvicorn` exit code 1 |
| SIGTERM | real app: `Shutting down` → `Results API stopped` → `Application shutdown complete` → `Finished server process`; process exit status 143 (= 128 + SIGTERM; uvicorn re-raises the signal after its graceful shutdown) |

### A. M1 acceptance criteria (spec §12)

| Requirement | Evidence | Status | Notes |
|---|---|---|---|
| Model | `app/models.py`; `tests/unit/test_models.py`; `tests/integration/test_migrations.py` (schema inspected in PostgreSQL) | MET | 13 columns, NOT NULL/defaults per §4, check constraint, 4 indexes |
| Alembic migration | `migrations/versions/1afbeabf0f46_create_test_results_table.py`; `test_migration_round_trip`; manual step 4 | MET | Autogenerated, unchanged |
| POST endpoint | `app/routers/results.py`; `tests/unit/test_results_api.py`; `test_post_stores_row_with_generated_values`, `test_post_explicit_source_is_preserved` | MET | |
| GET endpoints (list, by id) | same router; unit tests; integration tests for filters, ordering, pagination, `total`, 200/404 | MET | |
| API key | `app/dependencies.py` (`require_api_key`); unit tests (missing, empty, wrong, non-ASCII key → 401; key never logged); manual step 6 (missing key → 401) | MET | Manually verified 2026-09-24 (step 6) |
| Validation | `app/schemas.py`; `tests/unit/test_schemas.py`; unit endpoint tests (422) | MET | |
| `/health`, `/ready`, `/metrics` | `app/routers/health.py`, `app/metrics.py`; `tests/unit/test_operations.py`; `test_ready_returns_200_with_real_database` | MET | |
| §5.4 counters | `evp_results_received_total`, `evp_results_rejected_total` in `app/metrics.py`; unit tests; `test_received_counter_increases_after_real_insert` | MET | `test_name` label cardinality: accepted risk B5/D4 (see [Known limitations](#known-limitations-and-m4-traceability)) |
| HTTP metrics (count, latency, status per route) | `prometheus-fastapi-instrumentator` in `app/metrics.py`; `test_standard_http_metrics_are_recorded` | MET | Probe/scrape paths excluded |
| Logging `text` / `json` | `app/logging_config.py`; `tests/unit/test_logging_config.py` | MET | |
| Config validation | `app/config.py`; `tests/unit/test_config.py`; fail-fast run (exit 1) | MET | |
| Done when: `alembic upgrade head` works on an empty DB | Manual step 4 (`evp`); integration suite (`evp_test`), incl. upgrade from `base` in the round trip | MET | |
| Done when: valid POST returns 201 | Unit test with fake session; integration test with PostgreSQL; manual step 6 | MET | Manually verified 2026-09-24 (step 6) |
| Done when: invalid input returns 422 | Unit tests; `test_rejected_post_stores_nothing` (no row stored); manual step 6 on 2026-09-24 (`"verdict":"OK"` → 422, verdict must be `PASS` or `FAIL`) | MET | |
| Done when: `/ready` returns 503 when the DB is stopped | Automated: `test_unreachable_database_gives_503_without_leaking_details` (closed port) and unit tests; step 7B (unreachable host); step 7A manual stopped-container test | MET | Stopped-container behaviour (503, `/health` still 200) and recovery after PostgreSQL restart without restarting the API verified manually on 2026-09-24 (step 7A) |
| Done when: `/metrics` shows `evp_results_received_total` | `test_metrics_endpoint_uses_prometheus_format`, `test_received_counter_increases_after_real_insert` | MET | |
| Done when: unit + integration tests pass | Verification run above: 277 unit passed, 32 integration passed | MET | Measured 2026-09-24 on `docs/results-api-m1` @ `9e5d494` |

### B. Platform contract (spec §2)

The M1 platform-contract table is superseded by the
[Platform contract review (M4)](#platform-contract-review-m4) below; the M1 version remains in the
Git history.

**Platform files created during M1:** none. On 2026-09-24 the repository was searched (excluding
`.git` and `.venv`) for Dockerfiles, `docker-compose*`/`compose*.yml`, `k8s/`, `helm/`/`Chart.yaml`,
`kustomization.yaml`, `terraform/`/`*.tf`, `.github/workflows/`, `monitoring/`, `*.sql`
(database roles) and backup files: **no matches**. The only Docker usage is the local
`docker run` command documented above.

---

## Platform contract review (M4)

Application implementation reviewed through commit `94cc3ae` against spec §2, on 2026-10-22.
**Status** describes the application side. Deployment items (images, Kubernetes, CI, monitoring)
are platform work; they are listed as follow-up and are not assessed here. Evidence types:
*automated* (test names), *MANUAL M4* (2026-10-22, see [M4 verification](#m4-verification)),
*historical MANUAL M1* (2026-09-24, see above).

| # | Rule (spec §2) | Status | Evidence | Platform follow-up |
|---|---|---|---|---|
| 1 | All configuration via env vars | MET | `app/config.py` (`MigrationSettings`, `Settings`); `alembic.ini` has no URL; `.env.example`. `DB_CONNECT_TIMEOUT_S = 3` is a specified constant, not a host/port/secret (B11) | ConfigMap for non-secret values, Secret for `DB_PASSWORD` and `RESULTS_API_KEY` |
| 2 | Fail fast on bad config | MET | Automated: `tests/unit/test_config.py`, `tests/unit/test_migration_env.py` (the message names the variable, never the value; exit 1; also for `alembic`). Historical MANUAL M1: real app without configuration exits 1 | – |
| 3 | Stateless | MET | No local files; all data in PostgreSQL. Prometheus counters are per-process memory and reset on restart (normal for Prometheus) | – |
| 4 | Logs to stdout, secrets never logged, format selectable | MET | `app/logging_config.py`; automated: `tests/unit/test_logging_config.py` (incl. `test_log_level_applies_to_uvicorn_loggers`, `test_successful_probe_access_lines_are_suppressed`, `test_json_formatter_omits_only_uvicorn_color_message`). MANUAL M4: no access lines for successful `/health` and `/ready` | Log collection from stdout |
| 5 | `/health` (no DB), `/ready` (`SELECT 1`) | MET | `app/routers/health.py`; automated: `tests/unit/test_operations.py`, integration `test_ready_returns_200_with_real_database`. MANUAL M4: both HTTP 200. Historical MANUAL M1 step 7A: 503 while PostgreSQL was stopped, recovery without restart | Liveness/readiness probes; probe `timeoutSeconds` longer than the 3 s connect timeout |
| 6 | `/metrics`; exactly one uvicorn worker per container | MET | `app/metrics.py`; automated: `test_metrics_endpoint_uses_prometheus_format`, `test_received_counter_increases_after_real_insert`. One worker per container is a spec requirement (B8, see [Starting the API](#starting-the-api)). `test_name` label cardinality is an accepted risk (B5/D4) | Prometheus scrape config; run command without `--workers`, scale with replicas |
| 7 | Migrations are a separate command, never on startup | MET | `alembic upgrade head` only; `create_app()` never migrates (`test_importing_main_has_no_side_effects`); `env.py` loads only `MigrationSettings` (B1; `test_env_py_loads_only_the_migration_settings`, `test_offline_migration_works_without_api_only_variables`). MANUAL M4: `alembic upgrade head` / `alembic current` with `PORT` and `RESULTS_API_KEY` removed → `1afbeabf0f46 (head)` | Migration Job before deployment, with `DB_*` and logging settings only (no API-key Secret) |
| 8 | Graceful shutdown on SIGTERM | MET | uvicorn default; lifespan disposes the engine (`test_lifespan_creates_session_factory_and_disposes_engine`). Historical MANUAL M1 SIGTERM run. MANUAL M4: `kill -TERM` → full graceful shutdown sequence, no traceback | Termination grace period; draining of in-flight requests under load and Kubernetes termination not verified |
| 9 | Build once, deploy everywhere | MET | No environment-specific code paths; only env vars differ (`APP_ENV` affects logs only) | One image for dev and prod |
| 10 | Unique URL prefix `/api/v1/results` | MET | Router prefix in `app/routers/results.py`; `test_swagger_ui_and_routes_are_exposed` | Ingress path routing |
| 11 | Swagger UI `/docs` enabled | MET | FastAPI default, no environment toggle; `test_swagger_ui_and_routes_are_exposed` | Verify `/docs` in the deployed environments |

## M4 verification

The application implementation was reviewed and tested through commit `94cc3ae`. These README
changes are documentation only.

### Automated (offline, run by Claude Code, 2026-10-22)

| Check | Result |
|---|---|
| `flake8` | no findings, exit 0 |
| `pytest -m "not integration"` | **441 passed, 33 deselected**, 1 warning (`StarletteDeprecationWarning`, B6), exit 0 |

### MANUAL (performed by Muhammad, 2026-10-22, local PostgreSQL)

| Check | Result |
|---|---|
| Integration suite `pytest -m integration -v` against `evp_test` (run first; the Reporting suite ran only after it had finished) | **33 passed, 441 deselected**, 1 warning (B6), 2.80 s, exit 0 |
| Migrations without API-only variables (`PORT`, `RESULTS_API_KEY` removed from the command environment) | `alembic upgrade head` without error; `alembic current` → `1afbeabf0f46 (head)` |
| API start (`LOG_LEVEL=INFO`, `127.0.0.1:8001`) | `Started server process [3083]`; `/health` and `/ready` → `{"status":"ok"}`, HTTP 200; no access lines for these successful probe requests |
| End-to-end ingestion (simulator `once`, batch 5) | Simulator: `5 sent \| 5 accepted`, exit 0. API log: 5 × `POST /api/v1/results … 201`. `GET /api/v1/results?limit=5` → HTTP 200, `total` 28 → 33, ids 29–33, all `"source":"simulator"` |
| SIGTERM (`kill -TERM 3083`, the PID printed for the uvicorn process) | `Shutting down` → `Waiting for application shutdown.` → `Results API stopped` → `Application shutdown complete.` → `Finished server process [3083]`; no traceback; shell exit status 143 (128 + SIGTERM); afterwards `ps -p 3083` → no process |

---

## Known limitations and M4 traceability

M4 findings are tracked in GitHub issue #3. IDs: `B#` = backlog item from issue #3, `N#` = new
finding of the M4 audit, `D#` = M4 decision.

### Resolved in M4 (Results API)

| ID | Finding | Resolution | Commit |
|---|---|---|---|
| B1 | Alembic required the API-only `PORT` and `RESULTS_API_KEY` | `env.py` loads only `MigrationSettings` | `a4cf836` |
| B13 | Integration tests imported helpers from `tests/unit/` | Shared helpers moved to `tests/common.py` | `a4cf836` |
| B2 / N2 | A failure while reading back the row **after** the commit could return a `5xx` for a stored result, so a retrying client stored it twice | The commit is the last database step (see [API](#api)); the residual risk is listed below (D1) | `99716dc` |
| B3 (D2) | Empty, whitespace-only or arbitrarily long `source` accepted | 1–100 characters, not whitespace-only, stored unchanged | `d601c0b` |
| B4 (D3) | Inverted limit pair accepted | `limit_min > limit_max` → 422 (consistency check only, no verdict logic) | `d601c0b` |
| B20 (D6) | `GET` with `from >= to` returned an empty list | 422 `{"detail":"from must be earlier than to"}`, the same as the Reporting API (**breaking change**) | `d601c0b` |
| N3 | `started_at` before year 1 in UTC was accepted | Must be representable as a UTC timestamp → otherwise 422 | `d601c0b` |
| B12 | GET error codes missing from the spec | Documented in spec §5.3 | `7aefc83` |
| B9 | Non-SQLAlchemy exceptions gave a plain-text 500 | JSON `{"detail":"Internal server error"}` for every unexpected error | `4520d3c` |
| B7 (D5) | Successful probe/scrape requests filled the access log | Access lines for `/health`, `/ready`, `/metrics` with status < 400 are not written | `4520d3c` |
| N4 | uvicorn's `color_message` leaked into JSON log lines | Omitted from JSON output | `4520d3c` |
| N10 | `LOG_LEVEL` did not apply to uvicorn's loggers | uvicorn loggers follow `LOG_LEVEL` | `4520d3c` |

### Operational requirements and specified constraints

- **Exactly one uvicorn worker per container (B8).** Spec §2 rule 6: the metrics live in the
  memory of one process; scale with replicas, never with `--workers`. This is a requirement for
  the run command (platform), not an open defect.
- **Fixed 3 s database connect timeout (B11).** `DB_CONNECT_TIMEOUT_S = 3` is a code constant on
  purpose: the simulator's 10 s read timeout relies on it (spec §8.3.2), so a database outage
  reaches the client as `503`. B11 was raised as a question whether it should be configurable;
  that was intentionally rejected (accepted, documented no-change decision).

### Accepted risks and trade-offs

- **Residual duplicate risk (D1).** If the COMMIT succeeds but its confirmation is lost, the
  client receives `503` and a retry can store the result twice. Only request idempotency (not in
  scope) would remove it.
- **`test_name` label cardinality (B5/D4).** The label value comes from the client (1–100
  characters), so the API does not bound the number of series. Accepted: only clients holding the
  write API key can create values, and a value is recorded only after a result has been stored
  (spec §5.4).
- **Deprecation warning (B6).** Starlette's TestClient warns that `httpx` is deprecated in favour
  of `httpx2`. It affects only the tests (dev dependency); revisit during dependency maintenance.
- **Duplicated infrastructure code (B18).** `config.py`, `logging_config.py`, `db.py`, health and
  metrics setup are intentionally copied in the Reporting API, so each service stays
  self-contained.
- **Configuration errors are plain text (N7).** The fail-fast message is printed before the log
  format is known, so it is plain text even with `LOG_FORMAT=json`.
- **No lower bound on `started_at` (N8).** Only UTC representability and the 5-minute future limit
  are checked; there is no data-quality lower bound.

### Deferred platform / CI work (not implemented)

- **TCP keepalive (B10).** If a pooled connection's network path dies silently (e.g. NAT or a load
  balancer in front of a managed database), `pool_pre_ping` may wait for the operating-system TCP
  timeout. libpq keepalive / `tcp_user_timeout` settings are future Terraform/cloud work.
- **CI ordering (B19).** CI must apply the migrations, then run this integration suite, then the
  Reporting suite, sequentially (both use `evp_test`, and this suite `TRUNCATE`s it).
- **Deployment items** from the platform-contract table: ConfigMap/Secret, probes, migration Job,
  Prometheus scraping, single-worker run command, Ingress, image build.
