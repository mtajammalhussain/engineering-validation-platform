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

**Simulator (CLI):** items 1–4, 8 and 9 apply to the simulator. Items 5, 6, 7, 10 and 11 are
API-only: the simulator runs **no HTTP server** (no `/health`, `/ready`, `/metrics`, `/docs`) and
has no database. It only calls `POST /api/v1/results`. No server is added merely to satisfy these
items. Its fail-fast exit code is `2` (see §8.3.3).

---

## 3. Repository layout

```
engineering-validation-platform/
├── services/                      # application layer
│   ├── results-api/
│   │   ├── app/ (main.py, config.py, db.py, dependencies.py, models.py, schemas.py,
│   │   │         metrics.py, logging_config.py, routers/results.py, routers/health.py)
│   │   ├── migrations/            # Alembic (env.py, versions/)
│   │   ├── alembic.ini            # no database URL or credentials
│   │   ├── tests/unit/            # no database: conftest.py, fakes.py, test_*.py
│   │   │                          #   (incl. test_integration_guard.py)
│   │   ├── tests/integration/     # real PostgreSQL: conftest.py, support.py, test_*.py
│   │   ├── requirements.txt  requirements-dev.txt  setup.cfg  README.md
│   ├── reporting-api/             # same structure, but: no migrations/ or alembic.ini;
│   │                              #   models.py only describes the read-only table;
│   │                              #   adds calculations.py, queries.py, routers/reports.py
│   └── simulator/
│       ├── simulator/ (__main__.py, main.py, config.py, logging_config.py, catalog.py,
│       │               verdict.py, generator.py, client.py, output.py)
│       │              # started with: python -m simulator
│       ├── tests/unit/            # no database, no network: fakes.py, test_*.py
│       │                          #   (no tests/integration/: the simulator has no database)
│       └── requirements.txt  requirements-dev.txt  setup.cfg  README.md
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
| `received_at` | `timestamptz` not null default `now()` | set by the server |
| `source` | `text` not null default `'simulator'` | which bench sent it |

Indexes: `started_at`, `device_id`, `test_name`, `verdict`.

Example rows:

| id | device_id | test_name | temperature_c | measured_value | unit | verdict |
|---|---|---|---|---|---|---|
| 1 | ECU-001 | Sleep Current | -30 | 0.18 | mA | PASS |
| 2 | ECU-002 | Sleep Current | -40 | 0.52 | mA | FAIL |
| 3 | ECU-003 | Wake-up Time | 23 | 97.0 | ms | PASS |

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

- `device_id` matches `^ECU-[0-9]{3}$`.
- `test_name` 1–100 characters; `unit` 1–10 characters.
- `temperature_c` between −40 and 125 inclusive.
- `measured_value`, `limit_min`, `limit_max`: finite numbers (limits optional).
- `verdict` is exactly `PASS` or `FAIL`.
- `duration_s`: finite number > 0.
- `started_at` must be timezone-aware (naive timestamps rejected) and not more than 5 minutes in the future.
- Unknown fields are rejected (`extra = "forbid"`).

Response `201`: the stored result including `id` and `received_at`.
Errors: `401` missing/invalid API key, `422` validation, `503` database unavailable,
`500` unexpected server/database error (generic body; details are logged server-side only).

### 5.3 `GET /api/v1/results`

Query params: `device_id`, `test_name`, `verdict`, `from`, `to` (ISO 8601, tz-aware),
`limit` (default 50, max 500), `offset` (default 0). Sorted by `started_at` descending.
Time window: `from` is inclusive, `to` is exclusive (`from <= started_at < to`).
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

Works with a **database user that only has SELECT** privileges. No writes, no migrations. The
`test_results` table (§4) is owned by the Results API and its Alembic migrations; the Reporting API
only reads it.

**Read-only by design (defence in depth):** Reporting API connections default to read-only
transactions (`default_transaction_read_only=on`). This protects against accidental writes by
ordinary application code, but it is only a transaction default and can be overridden by a
session. The SELECT-only PostgreSQL role remains the actual security boundary required for M2
acceptance (§12).

### 7.1 Common query parameters and report window

| Param | Rule |
|---|---|
| `from`, `to` | Optional. Timezone-aware ISO 8601; naive timestamps → `422` |
| `device_id`, `test_name` | Optional exact-match filters, accepted by every report endpoint (also by `by-device` and `by-test`). No format check; an unknown value gives an empty result |

Time windows use the same semantics as §5.3: `from` is inclusive, `to` is exclusive
(`from <= started_at < to`). The default is a **rolling** seven-day window (exact times, not aligned
to calendar days); `now` is the request time in UTC:

| Given | Effective window |
|---|---|
| neither | `[now - 7 days, now)` |
| only `from` | `[from, now)` |
| only `to` | `[to - 7 days, to)` |
| both | `[from, to)` |

If the effective `from >= to` → `422`.

Every report response returns the effective `from` and `to` as timezone-aware **UTC** values
(`...Z`), also when the client sent another offset (e.g. `+02:00`).

### 7.2 Pass rate

`pass_rate_percent` = `passed / total * 100`, rounded to 1 decimal with **half-up** rounding
(`ROUND_HALF_UP`, computed exactly in decimal, not with binary floats; e.g. `6.25` → `6.3`).
**If `total == 0` it is `null`.**

### 7.3 Endpoints

| Method | Path | Response |
|---|---|---|
| `GET` | `/api/v1/reports/summary` | `{"from","to","total","passed","failed","pass_rate_percent"}` |
| `GET` | `/api/v1/reports/by-device` | `{"from","to","items"}`; each item `{device_id,total,passed,failed,pass_rate_percent}`; sorted by `failed` desc, then `device_id` asc |
| `GET` | `/api/v1/reports/by-test` | same, keyed by `test_name`; sorted by `failed` desc, then `test_name` asc |
| `GET` | `/api/v1/reports/timeseries?interval=hour\|day` | `{"from","to","items"}`; each item `{bucket_start,total,passed,failed}`; sorted by `bucket_start` asc |
| `GET` | `/health`, `/ready`, `/metrics` | see §2 |
| `GET` | `/docs` | Swagger UI |

- Grouped reports and timeseries return only groups/buckets that contain at least one result, so
  `items` may be `[]`. A `null` pass rate therefore only occurs in `summary`.
- `interval` is **required** and must be `hour` or `day`; otherwise `422`.
- Timeseries buckets are computed in **UTC** (`date_trunc(<interval>, started_at, 'UTC')`),
  independent of the database session's time zone. `bucket_start` is a timezone-aware UTC value.
- Empty buckets are **not** zero-filled. The first and last bucket may be partial when `from`/`to`
  do not fall on a bucket boundary (so `bucket_start` can be earlier than `from`).

Errors:

- `422` for invalid report parameters, including naive timestamps, `from >= to`, and missing or
  invalid `interval`;
- `503` when the database is unavailable;
- `500` for other unexpected server/database errors, with a generic response body; details are
  logged server-side only.

Example summary:

```json
{"from":"2026-09-16T00:00:00Z","to":"2026-09-23T00:00:00Z","total":1250,"passed":1182,"failed":68,"pass_rate_percent":94.6}
```

Example by-device:

```json
{"from":"2026-09-16T00:00:00Z","to":"2026-09-23T00:00:00Z","items":[{"device_id":"ECU-002","total":64,"passed":58,"failed":6,"pass_rate_percent":90.6},{"device_id":"ECU-001","total":61,"passed":61,"failed":0,"pass_rate_percent":100.0}]}
```

Aggregations are done **in SQL** (`COUNT`, `FILTER`, `GROUP BY`, `date_trunc`), not in Python loops.
The service never fetches raw result rows for a report: PostgreSQL returns one row per summary,
group or bucket, and Python only derives `pass_rate_percent` from those aggregated counts.

### 7.4 Metrics

Standard HTTP metrics only (request count, latency and status codes per route, e.g.
`prometheus-fastapi-instrumentator`). There are no Reporting-specific custom counters.

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
- `NaN` / `±inf` → `ValueError`, for the measured value **and** for any given limit.
- `limit_min > limit_max` (both given) → `ValueError`.

### 8.3 Behaviour

Overview of one batch:

1. Generate `SIM_BATCH_SIZE` results (§8.3.1).
2. Decide each verdict with `evaluate()` and include limits + verdict in the POST.
3. `POST` each result with the `X-API-Key` header, with limited retries (§8.3.2).
4. After each result, output one line (§8.4): a result line if it was **accepted** (`201`),
   otherwise a `REJECTED` or `UNDELIVERED` line.
5. After each batch, output one summary line (§8.4).

#### 8.3.1 Result generation (`generator.py`)

For each result, in this order (so a seed gives a stable sequence): random test from the catalog,
random device `ECU-001 … ECU-{SIM_DEVICE_COUNT:03}`, temperature from
`[-40, -30, -20, 23, 85, 105]`, failure decision, measured value, `duration_s` uniform between
5 and 60 (1 decimal). `started_at` is the current time in UTC (timezone-aware), taken for each
result when it is generated.

**Measurement resolution.** The bench measures with a resolution of **0.01** of the unit: every
measured value is a whole number of hundredths (generated as an integer `k`, value `= k / 100`).
The **same** value is evaluated, sent and displayed, so the displayed value always agrees with
the verdict (no `0.40 mA … FAIL (limits: 0.01–0.40 mA)` caused by hidden decimals). Measured
values are never negative (all quantities are magnitudes).

**Failure probability.** A result is generated **outside** its limits with probability
`SIM_FAILURE_RATE`, otherwise **inside**. Exception, **Sleep Current**:

| Temperature | Effective failure probability |
|---|---|
| `temperature_c < 0` | `min(1.0, SIM_FAILURE_RATE × 1.5)` |
| `temperature_c >= 0` | `SIM_FAILURE_RATE` |

All other tests are unaffected. This is an intentionally **synthetic ECU validation fault
model**, not a model of semiconductor leakage: cold conditions increase the probability that the
ECU does not reach or maintain its intended low-current sleep state. With
`SIM_FAILURE_RATE = 0` no result fails.

**Value ranges** (`span = max − min`, all values on the 0.01 grid):

| Case | Generated value |
|---|---|
| Inside, two-sided | uniform in `[min, max]` |
| Exactly on a limit | 2 % of the inside results: `min` or `max` (50/50); one-sided: `max` |
| Outside, two-sided | 50/50 below or above. Above: `[max + 0.01, max + span/2]`. Below: `[max(0, min − span/2), min − 0.01]`; if that range is empty, above is used |
| One-sided maximum (e.g. Wake-up Time) | treated as `min = max/2` **for generation only** (never sent), so `span = 75`: inside `[75.00, 150.00]`, outside `[150.01, 187.50]` |

Derived generation bounds that do not fall on the 0.01 grid are rounded inward: lower bounds up
to the next hundredth, upper bounds down to the previous hundredth. Catalog limits themselves are
not changed. Example, Sleep Current: `max = 0.40`, `span = 0.39`, `max + span/2 = 0.595` → upper
bound `0.59`.

Examples: Sleep Current below → `0.00 mA`, above → `0.41–0.59 mA`. The catalog only contains
two-sided and maximum-only tests; generating values for a minimum-only test is not supported
(a unit test guards the catalog shape). `evaluate()` and the output formatting still support
minimum-only limits.

**Payload.** Exactly the fields of §5.2, nothing else: `device_id`, `test_name`,
`temperature_c`, `measured_value`, `unit`, `limit_min`, `limit_max` (`null` if the catalog has no
limit on that side), `verdict`, `started_at`, `duration_s`, and `source` = `"simulator"` (sent
explicitly).

**Seed.** `SIM_SEED` (optional) makes the generated data reproducible: the same seed gives the
same tests, devices, temperatures, values and durations. `started_at` is real time and is not
reproducible. The random generator is seeded once per process, so in `loop` mode the whole run
repeats, not each batch. Retries and backoff consume no random numbers, so network behaviour does
not change the generated data.

**Clock.** The Results API rejects `started_at` more than 5 minutes in the future (§5.2). If the
simulator host clock is ahead by more than that, every result is rejected with `422`.

#### 8.3.2 Sending and retries (`client.py`)

- `POST {RESULTS_API_URL}/api/v1/results` with header `X-API-Key`. The key is only ever placed
  in this header.
- HTTP timeouts: connect 3 s; read, write and pool 10 s. The read timeout is longer than the
  Results API's 3 s database connect timeout, so a database outage arrives as `503` instead of a
  read timeout.
- **At most 3 attempts in total** (1 initial + 2 retries). Exponential backoff: wait 1 s before
  the 2nd attempt and 2 s before the 3rd. No random jitter.
- Redirects are not followed.

| Situation | Retried? | Outcome |
|---|---|---|
| `201` | – | **accepted** |
| Connection could not be established (`httpx.ConnectError`, `ConnectTimeout`, `PoolTimeout`): the request never reached the server | yes | **undelivered** after the 3rd attempt |
| HTTP `5xx` | yes | **undelivered** after the 3rd attempt |
| Any other status (`4xx`, `3xx`, other `2xx`) | no | **rejected** |
| Failure **after** the request was sent (`ReadTimeout`, `ReadError`, `WriteTimeout`, `WriteError`, `RemoteProtocolError`) | **no** | **undelivered** (may have been stored) |

Why failures after sending are not retried: `POST` has no idempotency key, so the server may
already have committed the result; a retry could store it twice and distort statistics. Losing
one synthetic result is harmless; a duplicate is not. Residual risk: a `5xx` returned after the
row was committed (narrow window) can still lead to a duplicate on retry.

Response bodies are **never** printed or logged. Logs contain only the status code, the attempt
number and the exception **class name** (not the exception text).

**Known limitation (M4 backlog):** there is no circuit breaker. During a Results API outage every
generated result goes through its own retry sequence, so a complete batch can take a long time.

#### 8.3.3 Modes, shutdown and exit codes

Modes (`SIM_MODE`):

- `once` (default): run one batch and exit. (Intended for a Kubernetes **CronJob**; can be
  triggered manually for demos with `kubectl create job --from=cronjob/<name> <job-name>`.)
  No signal handler is installed: SIGTERM terminates the process immediately.
- `loop` (for local Docker Compose): the first batch starts immediately; after each **completed**
  batch the simulator waits `SIM_INTERVAL_S` seconds (fixed delay, not fixed rate), then starts
  the next batch, until SIGTERM (or SIGINT, e.g. Ctrl+C). A batch with zero accepted results does
  not stop the loop.

Shutdown in `loop` mode: SIGTERM/SIGINT only set a stop flag. The wait between batches and the
retry backoff waits end **immediately** when the flag is set (no plain long sleep). A request that
is already in flight finishes, subject to the configured HTTP timeouts; no new result and no
further retry is started; the summary line of the partial batch is output; exit code `0`.
The platform termination grace period must allow enough time for an in-flight request to
complete; this is verified during the Docker/Kubernetes phase.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | `once`: at least one result accepted. `loop`: clean stop after SIGTERM/SIGINT |
| `1` | `once`: no result accepted |
| `2` | invalid configuration (fail fast, before anything is sent) |
| `3` | unexpected internal error (logged with traceback through the logger, so JSON logs stay JSON) |

### 8.4 Human-readable output (`output.py`)

```
ECU-001 | Test: Sleep Current | Temperature: -30°C | Measured current: 0.18 mA | Result: PASS
ECU-002 | Test: Sleep Current | Temperature: -40°C | Measured current: 0.52 mA | Result: FAIL (limits: 0.01–0.40 mA)
ECU-007 | Test: Wake-up Time | Temperature: 85°C | Measured time: 97.00 ms | Result: PASS
ECU-004 | Test: Wake-up Time | Temperature: 105°C | Measured time: 163.25 ms | Result: FAIL (limits: ≤ 150 ms)
ECU-001 | Test: Sleep Current | REJECTED: HTTP 401 (invalid or missing API key)
ECU-005 | Test: CAN Cycle Time | UNDELIVERED: ConnectError after 3 attempts
Batch done: 10 sent | 8 accepted | 1 rejected | 1 undelivered | 7 PASS | 1 FAIL
```

**Result line** (accepted results only):

- `Measured <quantity>:` uses the catalog `quantity` and `unit`; value with 2 decimals;
  temperature as integer with `°C`.
- On `FAIL`, the applied limits are appended as `(limits: …)`.
- **Limit precision:** limits are printed with the fewest decimals (0, 1 or 2) that show every
  limit of that test exactly; both limits of one test use the same number of decimals.
  Two-sided with an en dash (`–`), one-sided as `≤ <max>` / `≥ <min>` (with a space). Catalog
  results: `0.01–0.40 mA`, `80–250 mA`, `≤ 150 ms`, `9.5–10.5 ms`, `5.5–6.5 V`; a minimum-only
  limit would print as `≥ 5.5 V`.

**REJECTED line** (server answered with a non-retryable status): the reason is written by the
simulator, never taken from the response body:

- `REJECTED: HTTP 401 (invalid or missing API key)`
- `REJECTED: HTTP 422 (payload failed validation)`
- `REJECTED: HTTP <code> (unexpected response)` for any other status

**UNDELIVERED line** (no confirmation from the server):

- `UNDELIVERED: HTTP <code> after 3 attempts` (5xx on every attempt)
- `UNDELIVERED: <ExceptionClass> after 3 attempts` (connection failure on every attempt)
- `UNDELIVERED: <ExceptionClass>, not retried (may have been stored)` (failure after sending)
- `UNDELIVERED: shutdown requested before retry` (`loop` mode only)

**Summary line:** all fields are always present, also when `0`.

- `sent` = results the batch attempted to send (including rejected and undelivered ones).
- `PASS` / `FAIL` count **accepted** results only.
- Invariants: `sent = accepted + rejected + undelivered` and `accepted = PASS + FAIL`.

All lines are produced by pure functions (e.g. `format_result_line(...) -> str`, limit, rejected,
undelivered and summary formatting). How they are written to stdout is defined in §9.

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

Simulator validation (fail fast, exit code `2`; the error names the variable, never its value):

| Variable | Rule | Default |
|---|---|---|
| `RESULTS_API_URL` | required; scheme `http` or `https`; host required; no user/password, query or fragment; path empty or `/` (the client appends `/api/v1/results`) | — |
| `RESULTS_API_KEY` | required secret; at least 1 character; visible ASCII only (`!` … `~`), because an HTTP header cannot carry other characters | — |
| `SIM_MODE` | `once` or `loop` | `once` |
| `SIM_BATCH_SIZE` | integer 1–1000 | `10` |
| `SIM_INTERVAL_S` | integer 1–86400 (validated in both modes) | `60` |
| `SIM_FAILURE_RATE` | finite number 0.0–1.0 inclusive | `0.08` |
| `SIM_DEVICE_COUNT` | integer 1–999 (`ECU-###` has three digits) | `20` |
| `SIM_SEED` | unset **or empty** (`SIM_SEED=`) → no seed; otherwise integer ≥ 0 | unset |
| `APP_ENV`, `LOG_LEVEL`, `LOG_FORMAT` | same values as the APIs | `dev`, `INFO`, `text` |

`LOG_FORMAT`:

- `text` (default, local development): plain human-readable lines
  (`<time> | <level> | <logger> | <message>`).
- `json` (Kubernetes): one JSON object per line; the human-readable line goes into `msg`, structured
  fields stay separate for filtering. JSON is written with `ensure_ascii=False`, so characters such
  as `°`, `–`, `≤`, `≥` appear literally (not as `\u` escapes):

```json
{"ts":"2026-09-23T19:30:43Z","level":"INFO","service":"simulator","env":"dev","msg":"ECU-001 | Test: Sleep Current | Temperature: -30°C | Measured current: 0.18 mA | Result: PASS","device_id":"ECU-001","test_name":"Sleep Current","verdict":"PASS"}
```

Simulator output model: all §8.4 lines (result, `REJECTED`, `UNDELIVERED`, batch summary) are log
records of the logger `simulator.results`, whose `msg` is exactly the §8.4 text. There is no
separate `print()` output.

| | `LOG_FORMAT=text` | `LOG_FORMAT=json` |
|---|---|---|
| `simulator.results` | the **bare** §8.4 line only (no time, level or logger prefix) | `msg` = §8.4 line + structured fields |
| other simulator loggers (retry attempts, config, internal errors) | normal text format as the APIs | normal JSON format |

- Levels: result lines and summary `INFO`; `REJECTED`, `UNDELIVERED` and retry-attempt messages
  `WARNING`.
- Structured fields: result → `device_id`, `test_name`, `verdict`; rejected/undelivered →
  `device_id`, `test_name`, `outcome`, `status_code` (`null` when there was no HTTP response);
  summary → `sent`, `accepted`, `rejected`, `undelivered`, `passed`, `failed`.
  For UNDELIVERED because of a shutdown before a retry, `status_code` is the last HTTP status
  received, or `null` if no HTTP response was received.
- The `httpx` and `httpcore` loggers are set to `WARNING` or the global `LOG_LEVEL`, whichever is
  stricter. Their normal `INFO` request lines (and `DEBUG` details) are therefore suppressed, so
  the HTTP library adds no extra line per result, and the global threshold still applies (e.g.
  with `LOG_LEVEL=ERROR` their warnings are not shown either).

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
| 10 | inf | 0.01 | 0.40 | ValueError |
| 11 | 0.20 | NaN | 0.40 | ValueError (non-finite limit) |
| 12 | 0.20 | 0.40 | 0.01 | ValueError (min > max) |

**Simulator — `format_result_line()`:** a PASS line, a FAIL line with limits, a one-sided FAIL line.
Also: limit precision for every catalog test and a minimum-only limit (`≥ 5.5 V`); `REJECTED`,
`UNDELIVERED` and summary lines (including zero counts).

**Simulator — other unit tests** (no network: HTTP is replaced by `httpx.MockTransport`; no real
waiting: clock, random generator and waits are injected):

- Configuration: defaults, each invalid value from §9 → exit `2`, empty `SIM_SEED`, secret never
  shown.
- Generator: same seed → same data; every value on the 0.01 grid and never negative; failure rate
  `0` → only PASS, `1` → only FAIL; Sleep Current temperature rule; catalog shape.
- Client: retry/no-retry for every row of the §8.3.2 table, backoff delays `1, 2`, `X-API-Key`
  header sent.
- Modes: `once` exit codes `0`/`1`; `loop` stops on the stop flag; SIGTERM handler sets the flag.
- Logging: `text` mode prints bare `simulator.results` lines; `json` mode keeps `°`, `–`, `≤`, `≥`
  unescaped.

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
