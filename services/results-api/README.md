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
and records the M1 verification evidence.

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
- `migrations/env.py` loads the full application settings, so `PORT` and `RESULTS_API_KEY` must
  also be set when running Alembic (see [Known limitations](#known-limitations--m4-backlog)).

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
- Run **one** uvicorn worker process (do not add `--workers`); see
  [Known limitations](#known-limitations--m4-backlog).
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
`duration_s` finite and > 0; `started_at` timezone-aware and at most 5 minutes in the future;
unknown fields rejected. `source` may be omitted (PostgreSQL then stores `'simulator'`) but must
not be `null`. There is no limit logic: the verdict is stored as sent.

**GET list** — query parameters `device_id`, `test_name`, `verdict`, `from`, `to`
(timezone-aware ISO 8601; `from <= started_at < to`), `limit` (1–500, default 50),
`offset` (default 0). Sorted by `started_at` descending, then `id` descending.
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

### Error responses (as implemented)

| Status | When | Body |
|---|---|---|
| `401` | `POST` with missing or wrong `X-API-Key` | `{"detail":"Invalid or missing API key"}` |
| `404` | `GET /api/v1/results/{id}` with an unknown id | `{"detail":"Result not found"}` |
| `422` | Invalid POST body, invalid query parameters, invalid `{id}` (not an integer, < 1, above the PostgreSQL `bigint` maximum) | FastAPI's standard validation body: `{"detail":[...]}` |
| `503` | Database unreachable or unusable (SQLAlchemy `OperationalError`, `InterfaceError`, pool `TimeoutError`) on any `/api/v1/results` endpoint | `{"detail":"Database unavailable"}` |
| `503` | `/ready` when `SELECT 1` fails | `{"status":"unavailable"}` |
| `500` | Any other SQLAlchemy error (e.g. `IntegrityError`) | `{"detail":"Internal server error"}` |

Failed writes are rolled back. Database details (SQL, host, credentials, driver messages) are
logged server-side only and never returned. Exceptions that are **not** SQLAlchemy errors are not
mapped by the application (see [Known limitations](#known-limitations--m4-backlog)).

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
(see [Known limitations](#known-limitations--m4-backlog)).

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

## Manual verification

Manual checks complement the automated tests. The source of each check is stated explicitly.

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
| §5.4 counters | `evp_results_received_total`, `evp_results_rejected_total` in `app/metrics.py`; unit tests; `test_received_counter_increases_after_real_insert` | MET | See `test_name` label cardinality item in M4 backlog |
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

"App" rows cover what the application implements. "Platform" rows cover deployment work
(images, Kubernetes, CI, monitoring), which has **not started**; the application being
compatible with a rule does not make the deployment part MET.

| Requirement | Evidence | Status | Notes |
|---|---|---|---|
| 1. All configuration via env vars — app | `app/config.py` (`Settings`); `alembic.ini` has no URL; `.env.example` | MET | `DB_CONNECT_TIMEOUT_S = 3` is a code constant (a timeout, not a host/port/secret) |
| 1. — platform (ConfigMap / Secret) | none yet | NOT MET | Platform layer |
| 2. Fail fast on bad config | `load_settings()`; `tests/unit/test_config.py`; real app exits 1 listing missing variables | MET | Messages name variables, never values |
| 3. Stateless service | No local files written; all data in PostgreSQL; logs to stdout | MET | Prometheus counters are per-process memory and reset on restart (normal for Prometheus) |
| 4. Logs to stdout, secrets never logged, format selectable | `app/logging_config.py`; unit tests (stdout, text/json, key not logged, config errors without values) | MET | uvicorn loggers use the same format |
| 5. `/health` (no DB) and `/ready` (DB) — app | `app/routers/health.py`; unit + integration tests | MET | For results-api; reporting-api is M2 |
| 5. — platform (Kubernetes liveness/readiness probes) | none yet | NOT MET | Probe `timeoutSeconds` must exceed the 3 s connect timeout |
| 6. `/metrics` in Prometheus format — app | `app/metrics.py`; unit + integration tests | MET | |
| 6. — platform (Prometheus scraping, Grafana) | none yet | NOT MET | |
| 7. Migrations separate, never on startup — app | `alembic upgrade head` is a separate command; `create_app()` does not migrate; `test_importing_main_has_no_side_effects` | MET | |
| 7. — platform (migration Job before deployment) | none yet | NOT MET | Job would currently also need `PORT`/`RESULTS_API_KEY` (M4 item) |
| 8. Graceful shutdown on SIGTERM | uvicorn default; lifespan disposes the engine (`test_lifespan_creates_session_factory_and_disposes_engine`); real SIGTERM run (see verification run) | PARTIAL | Shutdown sequence verified; draining of in-flight requests under load and Kubernetes termination not verified |
| 9. Build once, deploy everywhere | No environment-specific code paths (only env vars differ; `APP_ENV` affects logs only) | PARTIAL | No image exists yet; not provable before image/deployment work |
| 10. Unique URL prefix `/api/v1/results` — app | `app/routers/results.py` (router prefix); OpenAPI path test | MET | |
| 10. — platform (Ingress path routing) | none yet | NOT MET | |
| 11. Swagger UI `/docs` enabled — app | FastAPI default, no environment toggle; `test_swagger_ui_and_routes_are_exposed` | MET | Availability in deployed environments not yet verified |

**Platform files created during M1:** none. On 2026-09-24 the repository was searched (excluding
`.git` and `.venv`) for Dockerfiles, `docker-compose*`/`compose*.yml`, `k8s/`, `helm/`/`Chart.yaml`,
`kustomization.yaml`, `terraform/`/`*.tf`, `.github/workflows/`, `monitoring/`, `*.sql`
(database roles) and backup files: **no matches**. The only Docker usage is the local
`docker run` command documented above.

---

## Known limitations / M4 backlog

Full M4 backlog: GitHub issue #3

These are known limitations and review items, not confirmed bugs:

- **Alembic settings.** `migrations/env.py` loads the full application settings, so migrations
  also require `PORT` and `RESULTS_API_KEY`, which they do not use.
- **POST idempotency.** There is no idempotency key. If a result is committed but the client does
  not receive the `201` (e.g. network loss, or an error after the commit), a client retry stores
  the result a second time. Requires review.
- **`source` policy.** Omitted and `null` are handled; an empty string and arbitrarily long values
  are currently accepted. Minimum/maximum length policy requires review.
- **`limit_min > limit_max`.** An inverted limit pair is currently accepted. Whether this is a
  data-quality error (422) requires review.
- **Prometheus `test_name` label.** Required by spec §5.4, but its value comes from the client
  (1–100 characters), so the number of series is not bounded by the API. Requires review
  (e.g. allow-list).
- **Deprecation warning.** Starlette's TestClient warns that `httpx` is deprecated in favour of
  `httpx2` (dev dependency only; tests pass).
- **Probe access logs.** `/health`, `/ready`, `/metrics` are excluded from the Prometheus HTTP
  metrics, but they still appear in the uvicorn access log; suppression is not implemented.
- **Single worker.** Prometheus metrics assume one uvicorn worker per container. Multiprocess
  mode is not implemented; do not start with `--workers`.
- **Non-SQLAlchemy exceptions.** Unexpected exceptions that are not SQLAlchemy errors are not
  mapped by the application and may fall back to Starlette's plain-text `500 Internal Server Error`
  instead of the API's JSON error format.
- **Half-open TCP connections.** If a pooled connection's network path dies silently,
  `pool_pre_ping` may wait until the operating-system TCP timeout, because explicit TCP keepalive
  tuning is not implemented. (`connect_timeout` limits only opening new connections.)
