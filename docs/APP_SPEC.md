# Engineering Validation Platform (EVP) — Application Specification

This document specifies the **application layer** of the Engineering Validation Platform: its
services, APIs, data model, configuration and tests.

The application is **intentionally minimal**. The focus of the project is the **DevOps platform
layer** around it: containerisation, dev/prod environments, Kubernetes, CI/CD, Infrastructure as
Code, monitoring, security and disaster recovery (see §11).

---

## 1. Purpose

A small system that collects and analyses **automated ECU test results** from test benches.

- A **simulator** plays the role of a test bench: it "measures", compares against its own limits,
  decides **PASS/FAIL**, and sends the finished result, like real benches that monitor limits
  and record verdicts themselves.
- The **Results API** receives, stores and returns individual results (ingestion).
- The **Reporting API** provides read-only statistics (analytics).

There is **no application frontend** in the mandatory scope. The application is demonstrated via
FastAPI's built-in **Swagger UI** (`/docs`), `curl`, logs, and **Grafana** operational dashboards
(see §10).

In a real company the simulator could be replaced by CANoe, a HIL bench, Jenkins or a Python
test framework. The platform does not care where results come from.

All data is **synthetic**. Test names, limits and device IDs are invented.

### Non-goals

- No requirement management, no test-spec database, no limit versioning.
- No user accounts / login (a simple API key protects writes, see §6).
- No web frontend (optional stretch goal only, see §12).

---

## 2. Architecture

```
                    ┌──────────────────────────────┐
                    │  Simulator (test bench)      │
                    │  Python CLI application,     │
                    │  designed to run as a        │
                    │  Kubernetes CronJob          │
                    │  measures + decides PASS/FAIL│
                    └──────────────┬───────────────┘
                                   │ HTTP POST /api/v1/results
                                   ▼
                    ┌──────────────────────────────┐
                    │  Results API (FastAPI)       │  ingestion
                    └──────────────┬───────────────┘
                                   │ read/write (evp_writer)
                                   ▼
                    ┌──────────────────────────────┐
                    │  PostgreSQL                  │
                    └──────────────┬───────────────┘
                                   │ SELECT only (evp_reader)
                                   ▼
                    ┌──────────────────────────────┐
                    │  Reporting API (FastAPI)     │  analytics
                    └──────────────────────────────┘

  Observability (separate concern):
  Results API /metrics ─┐
  Reporting API /metrics┼──▶ Prometheus ──▶ Grafana (operational dashboards)
  Kubernetes metrics ───┘
```

| Component | Tech | Default port | DB access |
|---|---|---|---|
| `results-api` | Python 3.12, FastAPI | 8001 | read/write, owns schema (Alembic) |
| `reporting-api` | Python 3.12, FastAPI | 8002 | **SELECT only** |
| `simulator` | Python 3.12 (CLI, httpx) | none | none |
| PostgreSQL | 16 | 5432 | n/a |

### Why two APIs?

- **Separation of responsibilities:** ingestion (many small writes from machines) vs. analytics
  (few, heavier read queries).
- **Failure isolation:** a slow report query cannot block result ingestion.
- **Least privilege:** the Reporting API uses a database user with **SELECT only**.
- **Independent scaling and deployment** of each service.

Known trade-off: both services share one database (a pragmatic compromise for this scope).

### Platform contract

These rules make the application run on Kubernetes without rework.

1. **All configuration via environment variables.** No hard-coded hosts, passwords, URLs or ports.
2. **Fail fast on bad config.** Required env vars are validated at startup (pydantic-settings). If one
   is missing, the service logs a clear error naming the variable and exits non-zero.
3. **Stateless services.** All persistent data lives in PostgreSQL. No local files.
4. **Logs to stdout**, never to files. Secrets are never logged. Format selectable (see §9).
5. **Health endpoints** on every API (not under `/api/v1`):
   - `GET /health` → `200 {"status":"ok"}` if the process is alive (liveness). No DB access.
   - `GET /ready` → `200` if the DB answers `SELECT 1`, otherwise `503` (readiness).
6. **Metrics:** `GET /metrics` in Prometheus text format on every API.
7. **Migrations are a separate command** (`alembic upgrade head`), **never** run on app startup.
8. **Graceful shutdown** on SIGTERM (uvicorn default for APIs; the simulator handles it in loop mode).
9. **Build once, deploy everywhere.** The same image runs in dev and prod; only env vars differ.
10. **Unique URL prefixes** so an Ingress can route by path:
    `/api/v1/results` → results-api, `/api/v1/reports` → reporting-api.
11. **Swagger UI** (`/docs`) stays enabled in all environments; it is the demo interface.

---

## 3. Repository layout

```
engineering-validation-platform/
├── services/                      # application layer
│   ├── results-api/
│   │   ├── app/ (main.py, config.py, db.py, models.py, schemas.py, metrics.py, logging_config.py, routers/results.py)
│   │   ├── migrations/            # Alembic
│   │   ├── tests/unit/  tests/integration/
│   │   ├── requirements.txt  requirements-dev.txt  setup.cfg  README.md
│   ├── reporting-api/             # same structure, no migrations
│   └── simulator/
│       ├── simulator/ (main.py, config.py, catalog.py, verdict.py, generator.py, client.py, output.py)
│       ├── tests/unit/
│       └── requirements.txt  requirements-dev.txt  README.md
│
├── docker-compose.yml             # platform layer (planned)
├── k8s/base/  k8s/overlays/{dev,prod}/   # platform layer: Kustomize (planned)
├── terraform/                     # platform layer (planned)
├── monitoring/                    # platform layer (planned)
├── .github/workflows/             # platform layer (planned)
│
├── docs/
│   └── APP_SPEC.md                # this file
├── .env.example                   # every env var, with safe dummy values
└── README.md
```

---

## 4. Data model (PostgreSQL)

One table: **`test_results`**

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` PK | |
| `device_id` | `text` not null | format `ECU-###` |
| `test_name` | `text` not null | e.g. `Sleep Current` |
| `temperature_c` | `double precision` not null | |
| `measured_value` | `double precision` not null | |
| `unit` | `text` not null | e.g. `mA`, `ms`, `V` |
| `limit_min` | `double precision` null | as reported by the bench (optional) |
| `limit_max` | `double precision` null | as reported by the bench (optional) |
| `verdict` | `text` not null, check in (`PASS`,`FAIL`) | **decided by the bench** |
| `started_at` | `timestamptz` not null | from the bench |
| `duration_s` | `double precision` not null | |
| `received_at` | `timestamptz` default `now()` | set by the server |
| `source` | `text` default `'simulator'` | which bench sent it |

Indexes: `started_at`, `device_id`, `test_name`, `verdict`.

Example rows:

| id | device_id | test_name | temperature_c | measured_value | unit | verdict |
|---|---|---|---|---|---|---|
| 1 | ECU-001 | Sleep Current | -30 | 0.18 | mA | PASS |
| 2 | ECU-002 | Sleep Current | -40 | 137.0 | mA | FAIL |
| 3 | ECU-003 | Wake-up Time | 25 | 97.0 | ms | PASS |

Database roles (`evp_writer`, `evp_reader`) are provisioned by the platform layer, not by
application code. The application works with whichever database user it is given.

---

## 5. Results API (`results-api`)

The Results API **stores and returns** results. It does **not** judge them.

### 5.1 Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/v1/results` | API key | Store one result |
| `GET` | `/api/v1/results` | none | List results (filters + pagination) |
| `GET` | `/api/v1/results/{id}` | none | One result |
| `GET` | `/health`, `/ready`, `/metrics` | none | See §2 |
| `GET` | `/docs` | none | Swagger UI |

### 5.2 `POST /api/v1/results`

Request:

```json
{
  "device_id": "ECU-003",
  "test_name": "Sleep Current",
  "temperature_c": -30,
  "measured_value": 0.22,
  "unit": "mA",
  "limit_min": 0.01,
  "limit_max": 0.40,
  "verdict": "PASS",
  "started_at": "2026-09-23T19:30:00Z",
  "duration_s": 42.0,
  "source": "simulator"
}
```

Validation (only data-quality checks, **no** limit logic) → `422` with a clear message:

- `device_id` matches `^ECU-\d{3}$`.
- `test_name` 1–100 characters; `unit` 1–10 characters.
- `temperature_c` between −40 and 125 inclusive.
- `measured_value`, `limit_min`, `limit_max`: finite numbers (limits optional).
- `verdict` is exactly `PASS` or `FAIL`.
- `duration_s` > 0.
- `started_at` must be timezone-aware (naive timestamps rejected) and not more than 5 minutes in the future.
- Unknown fields are rejected (`extra = "forbid"`).

Response `201`: the stored result including `id` and `received_at`.
Errors: `401` missing/invalid API key, `422` validation, `503` database unavailable.

### 5.3 `GET /api/v1/results`

Query params: `device_id`, `test_name`, `verdict`, `from`, `to` (ISO 8601, tz-aware),
`limit` (default 50, max 500), `offset` (default 0). Sorted by `started_at` descending.
Response: `{"items": [...], "total": <int>, "limit": 50, "offset": 0}`.

Example: all failed Sleep Current tests of ECU-004:
`GET /api/v1/results?device_id=ECU-004&test_name=Sleep%20Current&verdict=FAIL`

### 5.4 Metrics (in addition to standard HTTP metrics)

- `evp_results_received_total{test_name, verdict}` (counter): used by Grafana for
  "results per minute" and "PASS/FAIL rate" panels
- `evp_results_rejected_total{reason}` (counter; e.g. `validation`, `auth`)
- HTTP request count, latency and status codes per route (e.g. `prometheus-fastapi-instrumentator`)

---

## 6. Authentication (writes only)

- `POST /api/v1/results` requires header `X-API-Key: <value>`.
- The expected value comes from env var `RESULTS_API_KEY` (provided as a Kubernetes Secret).
- Compared with `secrets.compare_digest` (constant-time). The key is never logged.

---

## 7. Reporting API (`reporting-api`)

Works with a **database user that only has SELECT** privileges. No writes, no migrations.

Common query params: `from`, `to` (tz-aware ISO 8601; default: last 7 days), optional
`device_id`, `test_name`.

`pass_rate_percent` = `passed / total * 100`, rounded to 1 decimal. **If `total == 0` it is `null`.**

| Method | Path | Response |
|---|---|---|
| `GET` | `/api/v1/reports/summary` | `{"from","to","total","passed","failed","pass_rate_percent"}` |
| `GET` | `/api/v1/reports/by-device` | list of `{device_id,total,passed,failed,pass_rate_percent}`, sorted by `failed` desc |
| `GET` | `/api/v1/reports/by-test` | same, keyed by `test_name` |
| `GET` | `/api/v1/reports/timeseries?interval=hour\|day` | list of `{bucket_start,total,passed,failed}` |
| `GET` | `/health`, `/ready`, `/metrics` | see §2 |
| `GET` | `/docs` | Swagger UI |

Example summary:

```json
{"from":"2026-09-16T00:00:00Z","to":"2026-09-23T00:00:00Z","total":1250,"passed":1182,"failed":68,"pass_rate_percent":94.6}
```

Aggregations are done **in SQL** (`COUNT`, `FILTER`, `GROUP BY`, `date_trunc`), not in Python loops.

No CORS configuration is needed (there is no browser frontend).

---

## 8. Simulator (`simulator`) — the test bench

The simulator is a **Python CLI application**. Scheduling it as a Kubernetes CronJob is part of
the platform layer.

### 8.1 Built-in test catalog (`catalog.py`)

The bench owns its limits, as a simple constant in code. Invented values:

| key | test_name | quantity | unit | min | max |
|---|---|---|---|---|---|
| `SLEEP_CURRENT` | Sleep Current | current | mA | 0.01 | 0.40 |
| `ACTIVE_SUPPLY_CURRENT` | Active Supply Current | current | mA | 80 | 250 |
| `WAKEUP_TIME` | Wake-up Time | time | ms | *None* | 150 |
| `CAN_CYCLE_TIME` | CAN Cycle Time | time | ms | 9.5 | 10.5 |
| `UNDERVOLTAGE_RESET` | Undervoltage Reset | voltage | V | 5.5 | 6.5 |

### 8.2 Verdict logic (`verdict.py`)

A **pure function**, no I/O:

```python
def evaluate(measured_value: float, limit_min: float | None, limit_max: float | None) -> str  # "PASS" / "FAIL"
```

- Limits are **inclusive**: `limit_min <= value <= limit_max` → `PASS`, otherwise `FAIL`.
- `None` means no limit on that side. Both `None` → `ValueError`.
- `NaN` / `±inf` → `ValueError`.

### 8.3 Behaviour

1. Generate `SIM_BATCH_SIZE` results. For each: random test from the catalog, random device
   `ECU-001 … ECU-{SIM_DEVICE_COUNT:03}`, temperature from `[-40, -30, -20, 23, 85, 105]`,
   `started_at = now (UTC)`, `duration_s` between 5 and 60.
2. Value generation:
   - With probability `SIM_FAILURE_RATE`: a value **outside** the limits.
   - Otherwise a value **inside** the limits; occasionally (~2 %) **exactly on** a limit.
   - Colder temperatures slightly increase the failure probability for Sleep Current.
3. Decide the verdict with `evaluate()` and include limits + verdict in the POST.
4. `POST` with the `X-API-Key` header. Retry network errors/5xx with exponential backoff
   (max 3 attempts); no retry on 4xx.
5. After each **accepted** result (201), print one result line (§8.4). Rejected results print a
   REJECTED line.
6. After each batch, print a summary line.

Modes (`SIM_MODE`):

- `once` (default): run one batch and exit. Exit code `0` if ≥1 result accepted, else `1`.
  (Intended for a Kubernetes **CronJob**; can be triggered manually for demos with
  `kubectl create job --from=cronjob/<name> <job-name>`.)
- `loop`: repeat every `SIM_INTERVAL_S` seconds until SIGTERM (for local Docker Compose).

`SIM_SEED` (optional) makes the generated data reproducible.

### 8.4 Human-readable output (`output.py`)

```
ECU-001 | Test: Sleep Current | Temperature: -30°C | Measured current: 0.18 mA | Result: PASS
ECU-002 | Test: Sleep Current | Temperature: -40°C | Measured current: 137.50 mA | Result: FAIL (limits: 0.01–0.40 mA)
ECU-007 | Test: Wake-up Time | Temperature: 85°C | Measured time: 97.00 ms | Result: PASS
ECU-001 | Test: Sleep Current | REJECTED: <reason>
Batch done: 10 sent | 10 accepted | 9 PASS | 1 FAIL
```

- `Measured <quantity>:` uses the catalog `quantity` and `unit`; value with 2 decimals;
  temperature as integer with `°C`.
- On `FAIL`, the applied limits are appended (one-sided limits as `≤ 150 ms` / `≥ 5.5 V`).
- Implemented as a pure function `format_result_line(...) -> str`.

---

## 9. Configuration & logging

| Variable | Used by | Example | Secret? |
|---|---|---|---|
| `APP_ENV` | all | `dev` / `prod` | no |
| `LOG_LEVEL` | all | `INFO` | no |
| `LOG_FORMAT` | all | `text` / `json` | no |
| `PORT` | APIs | `8001` | no |
| `DB_HOST`, `DB_PORT`, `DB_NAME` | APIs | `postgres`, `5432`, `evp` | no |
| `DB_USER` | APIs | `evp_writer` / `evp_reader` | no |
| `DB_PASSWORD` | APIs | — | **yes** |
| `RESULTS_API_KEY` | results-api, simulator | — | **yes** |
| `RESULTS_API_URL` | simulator | `http://results-api:8001` | no |
| `SIM_MODE`, `SIM_BATCH_SIZE`, `SIM_INTERVAL_S` | simulator | `once`, `10`, `60` | no |
| `SIM_FAILURE_RATE`, `SIM_DEVICE_COUNT`, `SIM_SEED` | simulator | `0.08`, `20`, *(unset)* | no |

Separate `DB_*` variables (instead of one URL) let non-secret values live in a ConfigMap and only the
password in a Secret. `.env.example` lists all variables with dummy values; real `.env` files are git-ignored.

`LOG_FORMAT`:

- `text` (default, local development): plain human-readable lines.
- `json` (Kubernetes): one JSON object per line; the human-readable line goes into `msg`, structured
  fields stay separate for filtering:

```json
{"ts":"2026-09-23T19:30:43Z","level":"INFO","service":"simulator","env":"dev","msg":"ECU-001 | Test: Sleep Current | Temperature: -30°C | Measured current: 0.18 mA | Result: PASS","device_id":"ECU-001","test_name":"Sleep Current","verdict":"PASS"}
```

---

## 10. Observability & demo

The monitoring stack (Prometheus, Grafana) is part of the platform layer. The application exposes
metrics (§5.4) and health endpoints (§2).

**Separation of concerns:**

| Data | Where it comes from | Shown via |
|---|---|---|
| Business data (results, PASS/FAIL statistics) | Reporting API / Results API | Swagger UI, curl |
| Application metrics (results/min, PASS/FAIL counters, HTTP latency, error rates) | `/metrics` → Prometheus | Grafana |
| Infrastructure metrics (CPU, memory, pod restarts, availability) | Kubernetes → Prometheus | Grafana |

Grafana does **not** query PostgreSQL directly (that would bypass the Reporting API and couple
Grafana to the table schema).

**Planned demo flow (~5 min):**

1. Trigger the simulator CronJob manually → show result lines in `kubectl logs`.
2. Swagger: `GET /api/v1/reports/summary` and a filtered `GET /api/v1/results`.
3. Grafana: "results per minute" and PASS/FAIL panels increase.
4. Delete a Results API pod → Kubernetes recreates it; the readiness probe keeps traffic away until ready.

---

## 11. Testing, code quality & scope

Tools: `pytest`, `pytest-cov`, `flake8`. Python 3.12. Exact versions pinned in `requirements*.txt`.
`psycopg2-binary` is used (no compiler needed in the image).

- **Unit tests** (`tests/unit`): no database, no network.
- **Integration tests** (`tests/integration`, marker `integration`): against a **real PostgreSQL**
  given by `TEST_DB_*` env vars (the CI pipeline provides PostgreSQL as a service container).
  No SQLite substitute, because the reporting SQL uses PostgreSQL features.
- `pytest -m "not integration"` passes without any database.

### Required unit tests

**Simulator — `evaluate()` (parametrized, boundary value analysis):**

| # | value | min | max | expected |
|---|---|---|---|---|
| 1 | 0.40 | 0.01 | 0.40 | PASS (on upper limit) |
| 2 | 0.401 | 0.01 | 0.40 | FAIL (just above) |
| 3 | 0.01 | 0.01 | 0.40 | PASS (on lower limit) |
| 4 | 0.0099 | 0.01 | 0.40 | FAIL (just below) |
| 5 | 150 | None | 150 | PASS (one-sided, on limit) |
| 6 | 150.1 | None | 150 | FAIL (one-sided, above) |
| 7 | 7.0 | 5.5 | 6.5 | FAIL |
| 8 | 1.0 | None | None | ValueError |
| 9 | NaN | 0.01 | 0.40 | ValueError |

**Simulator — `format_result_line()`:** a PASS line, a FAIL line with limits, a one-sided FAIL line.

**Results API — request validation:** valid payload accepted; `verdict: "OK"` → 422;
naive timestamp → 422; bad `device_id` → 422; missing API key → 401.

**Reporting API — pass rate:** 111/120 → 92.5; 0/0 → `null`.

### Application layer vs. platform layer

| Layer | Contents | Location |
|---|---|---|
| Application | Service code, migrations, unit/integration tests, `.env.example`, service READMEs | `services/` |
| Platform | Dockerfiles, Docker Compose, Kubernetes manifests (Kustomize dev/prod overlays), GitHub Actions CI/CD, Terraform, Prometheus/Grafana configuration, database roles, backups | `docker-compose.yml`, `k8s/`, `.github/workflows/`, `terraform/`, `monitoring/` |

---

## 12. Application milestones (acceptance criteria)

**M1 — Results API**
- Model, Alembic migration, POST/GET endpoints, API key, validation.
- Health/ready/metrics (incl. §5.4 counters), logging (`text`/`json`), config validation.
- ✅ Done when: `alembic upgrade head` works on an empty DB; a valid POST returns 201; invalid input
  returns 422; `/ready` returns 503 when the DB is stopped; `/metrics` shows
  `evp_results_received_total`; unit + integration tests pass.

**M2 — Reporting API**
- All report endpoints with SQL aggregations; `null` pass rate on empty data.
- ✅ Done when: works with a SELECT-only DB user; integration tests cover summary and by-device.

**M3 — Simulator**
- Catalog, `evaluate()`, generator, `once`/`loop` modes, retries, SIGTERM handling, output lines.
- ✅ Done when: `SIM_MODE=once` populates the DB, prints one line per result and a batch summary,
  exits 0; with a wrong API key it prints REJECTED lines and exits 1.

**M4 — Hardening**
- All services reviewed against the platform contract (§2); README per service.

**Stretch goals (optional, not part of the mandatory scope)**
- Web dashboard (e.g. React + Vite, served as static files; would require CORS/Ingress changes).
- HTTPS via Ingress + cert-manager.

---

## 13. Key design decisions

| Decision | Reason |
|---|---|
| The test bench (simulator) decides PASS/FAIL; the platform only stores and analyses | Matches real benches that monitor limits themselves; keeps the platform independent of test specifications |
| Two APIs (ingestion vs. analytics) | Failure isolation, independent scaling, least privilege (read-only DB user) |
| One shared PostgreSQL database | Pragmatic for this scope; separated by DB roles instead of separate databases |
| Separate `DB_*` variables instead of one connection URL | Non-secret values in a ConfigMap, only the password in a Secret |
| Migrations as a separate command, not on startup | Enables controlled schema changes; planned to run as a Kubernetes Job before deployment |
| No application frontend; Swagger UI for the demo | Keeps effort on the platform; Grafana stays dedicated to observability |
| Grafana reads Prometheus, not PostgreSQL | Clean separation of business data and operational monitoring |
