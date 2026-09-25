# Simulator (`simulator`)

The simulator plays the role of an **ECU test bench**. It generates synthetic measurements,
compares them against its own built-in limits, decides **PASS/FAIL** itself, and sends each
finished result to the Results API.

- It **decides** the verdict; the platform only stores and analyses results.
- It talks to the platform **only over HTTP** (`POST /api/v1/results`). It has **no database
  access** and no database credentials.
- It is a **command-line application**, not a web service: it has no `/health`, `/ready`,
  `/metrics` or Swagger endpoint.

The single source of truth for all technical requirements is
[`docs/APP_SPEC.md`](../../docs/APP_SPEC.md) (§8 describes the simulator). This README explains
how to run and operate it and records the M3 verification evidence.

```
Simulator (test bench, CLI)
    |
    |  POST /api/v1/results   (header X-API-Key)
    v
Results API
    |
    v
PostgreSQL
```

---

## Requirements

- **Python 3.12**
- A reachable **Results API** (see [`services/results-api/README.md`](../results-api/README.md)).
  PostgreSQL is only needed indirectly, by the Results API.

## Setup

```bash
cd services/simulator
python3.12 -m venv .venv                     # isolated Python environment for this service
.venv/bin/python -m pip install -r requirements-dev.txt
```

`requirements.txt` holds only what the running simulator needs (`pydantic-settings`, `httpx`);
`requirements-dev.txt` includes it and adds `pytest`, `pytest-cov` and `flake8`. All versions are
pinned exactly.

## Configuration

The simulator reads **only environment variables**; it does not open a `.env` file itself. The
shared, git-ignored `.env` lives at the repository root (created from `.env.example`). Load it
into your shell before running the simulator:

```bash
set -a; source ../../.env; set +a            # from services/simulator
```

| Variable | Required | Default | Rule | Purpose |
|---|---|---|---|---|
| `RESULTS_API_URL` | yes | – | `http`/`https`, host required, no user/password, no query/fragment, path empty or `/` (the simulator appends `/api/v1/results`) | Base URL of the Results API |
| `RESULTS_API_KEY` | yes | – | **secret**; at least 1 character; visible ASCII only (`!` … `~`) | Sent as `X-API-Key` header |
| `SIM_MODE` | no | `once` | `once` or `loop` | One batch, or batches until stopped |
| `SIM_BATCH_SIZE` | no | `10` | integer 1–1000 | Results per batch |
| `SIM_INTERVAL_S` | no | `60` | integer 1–86400 | Pause after each batch in `loop` mode |
| `SIM_FAILURE_RATE` | no | `0.08` | finite number 0.0–1.0 inclusive | Share of results generated outside the limits |
| `SIM_DEVICE_COUNT` | no | `20` | integer 1–999 | Devices `ECU-001` … `ECU-<count>` |
| `SIM_SEED` | no | unset | integer ≥ 0; empty = unset | Makes generated data reproducible |
| `APP_ENV` | no | `dev` | `dev` or `prod` | Appears in JSON logs |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` … `CRITICAL` | Log threshold |
| `LOG_FORMAT` | no | `text` | `text` or `json` | Output format (see [Logging](#logging)) |

If a variable is missing or invalid, the simulator prints one line per problem (variable name and
reason, **never the value**) and exits with code **2** before sending anything. The API key is held
as a secret value and is never logged.

**Local URL:** local examples use `127.0.0.1` explicitly to match the Results API's documented
uvicorn bind address (`--host 127.0.0.1`) and avoid hostname-resolution ambiguity on systems where
`localhost` may also resolve to IPv6.

## Running once

```bash
cd services/simulator
set -a; source ../../.env; set +a
RESULTS_API_URL="http://127.0.0.1:${PORT}" SIM_MODE=once .venv/bin/python -m simulator
echo "exit code: $?"
```

`python -m simulator` runs `simulator/__main__.py`, which only calls `simulator.main.main()`.

Exit codes (spec §8.3.3):

| Code | Meaning |
|---|---|
| `0` | At least one result was accepted (or, in `loop` mode, a clean stop) |
| `1` | `once` mode: no result was accepted |
| `2` | Invalid configuration (fail fast, before anything is sent) |
| `3` | Unexpected internal error, logged with traceback through the normal logger |

In `once` mode the simulator installs no signal handlers, so an external `SIGTERM` or `Ctrl+C`
ends the process with the operating system's/Python's default behaviour. That is outside the
0/1/2/3 codes above.

## Running continuously (`loop`)

In the same shell and directory as above (root `.env` loaded):

```bash
RESULTS_API_URL="http://127.0.0.1:${PORT}" SIM_MODE=loop SIM_INTERVAL_S=60 .venv/bin/python -m simulator
```

- The first batch starts immediately. After each **completed** batch the simulator waits
  `SIM_INTERVAL_S` seconds (fixed delay), then starts the next one.
- A batch without any accepted result does **not** stop the loop.
- `SIGTERM` or `SIGINT` (`Ctrl+C`) only sets a stop flag:
  - a request already in flight finishes (bounded by the HTTP timeouts);
  - no new result and no new retry is started; the wait between batches ends immediately;
  - if the batch had already started at least one result, its **partial** summary is printed;
    if the stop arrives before the first result of a batch, that batch never started and prints
    no summary;
  - the process exits with code `0`.

## Test catalog

The bench owns its limits (spec §8.1). Limits are inclusive: `min ≤ value ≤ max` is `PASS`.

| Test | Unit | Min | Max |
|---|---|---|---|
| Sleep Current | mA | 0.01 | 0.40 |
| Active Supply Current | mA | 80 | 250 |
| Wake-up Time | ms | – | 150 |
| CAN Cycle Time | ms | 9.5 | 10.5 |
| Undervoltage Reset | V | 5.5 | 6.5 |

Each result gets a random test, device (`ECU-001` … `ECU-<SIM_DEVICE_COUNT>`), temperature from
`[-40, -30, -20, 23, 85, 105]` °C and duration of 5.0–60.0 s. Measured values have a resolution of
0.01 of the unit, so the value that is evaluated, sent and printed is always the same. A value is
generated outside the limits with probability `SIM_FAILURE_RATE`; about 2 % of the passing values
lie exactly on a limit. One exception, a synthetic ECU validation fault model (not a physical
semiconductor leakage model): for **Sleep Current** at `temperature_c < 0` the probability is
`min(1.0, SIM_FAILURE_RATE × 1.5)`; at `temperature_c >= 0` it is `SIM_FAILURE_RATE`.

## Retry behaviour

Every result is sent with at most **3 attempts in total**, waiting **1 s** before the 2nd and
**2 s** before the 3rd attempt (no random jitter). HTTP timeouts: connect 3 s; read, write and
pool 10 s. Redirects are not followed.

| Situation | Retried? | Outcome |
|---|---|---|
| `201 Created` | – | accepted |
| Connection not established (`ConnectError`, `ConnectTimeout`, `PoolTimeout`) | yes | `UNDELIVERED` after 3 attempts |
| HTTP `5xx` | yes | `UNDELIVERED` after 3 attempts |
| Any other status (`4xx`, `3xx` redirects, other `2xx`) | no | `REJECTED` |
| Failure after the request was sent (`ReadTimeout`, `ReadError`, `WriteTimeout`, `WriteError`, `RemoteProtocolError`) | no | `UNDELIVERED` (may have been stored) |

Ambiguous after-send transport failures listed above are not retried because `POST` has no
idempotency key: the Results API may already have stored the result, and retrying could store it
twice. HTTP `5xx` responses are still retried according to the table above. Each actual retry is logged as a
`WARNING` (`Retrying result delivery after HTTP 503 (attempt 1/3)`).

## Output

One line per result, then one summary line per batch (spec §8.4; excerpt):

```
ECU-001 | Test: Sleep Current | Temperature: -30°C | Measured current: 0.18 mA | Result: PASS
ECU-002 | Test: Sleep Current | Temperature: -40°C | Measured current: 0.52 mA | Result: FAIL (limits: 0.01–0.40 mA)
ECU-004 | Test: Wake-up Time | Temperature: 105°C | Measured time: 163.25 ms | Result: FAIL (limits: ≤ 150 ms)
ECU-001 | Test: Sleep Current | REJECTED: HTTP 401 (invalid or missing API key)
ECU-005 | Test: CAN Cycle Time | UNDELIVERED: ConnectError after 3 attempts
Batch done: 10 sent | 8 accepted | 1 rejected | 1 undelivered | 7 PASS | 1 FAIL
```

- `REJECTED` = the server answered with a non-retryable status. The reason is chosen by status
  code only (`401`, `422`, otherwise "unexpected response"); response bodies are never printed.
- `UNDELIVERED` = the result was not confirmed as accepted by the server (repeated `5xx`,
  connection failures, a failure after sending, or a shutdown before a retry). For exceptions
  only the class name is shown.
- `PASS`/`FAIL` in the summary count **accepted** results only;
  `sent = accepted + rejected + undelivered`.

## Logging

All output goes to **stdout**; secrets are never logged.

- `LOG_FORMAT=text`: result and summary lines are printed **bare**, exactly as above. Other
  messages (retry warnings, errors) use `<time> | <level> | <logger> | <message>`.
- `LOG_FORMAT=json`: one JSON object per line with `ts`, `level`, `service` (`simulator`), `env`
  and `msg` (the human-readable line). Structured fields are added:
  - accepted result: `device_id`, `test_name`, `verdict`
  - rejected/undelivered result: `device_id`, `test_name`, `outcome`, `status_code`
    (`null` when there was no HTTP response)
  - summary: `sent`, `accepted`, `rejected`, `undelivered`, `passed`, `failed`

  Characters such as `°`, `–`, `≤` are written literally.

Example (from spec §9):

```json
{"ts":"2026-09-23T19:30:43Z","level":"INFO","service":"simulator","env":"dev","msg":"ECU-001 | Test: Sleep Current | Temperature: -30°C | Measured current: 0.18 mA | Result: PASS","device_id":"ECU-001","test_name":"Sleep Current","verdict":"PASS"}
```

## Reproducible data (`SIM_SEED`)

With `SIM_SEED` set, the same seed and the same simulation settings produce the same generated
sequence of tests, devices, temperatures, measured values, verdicts and durations.

- `started_at` is always the real current UTC time.
- The random generator is seeded once per process (in `loop` mode the whole run repeats, not each
  batch).
- Retries and network behaviour use no random numbers, so they cannot change the generated data.
  They can still add their own lines (retry warnings, `REJECTED`, `UNDELIVERED`); under the same
  successful delivery conditions the result and summary output is deterministic.

## Tests

```bash
cd services/simulator
.venv/bin/flake8
.venv/bin/python -m pytest -m "not integration" -q
```

Unit tests only: no database, no network (HTTP is replaced by `httpx.MockTransport`), no real
waiting and no real signals. The simulator has no integration tests because it has no database;
the end-to-end behaviour is verified manually (below).

Verification evidence (M3 Stage 5/6, Python 3.12.14): `375 passed`, also with warnings treated as
errors and with invalid simulator variables set in the surrounding shell; `flake8` clean. The
number of tests will change as the code evolves.

---

## M3 acceptance evidence (manual end-to-end)

Performed on 2026-09-26 on a developer machine against the **real** Results API (started locally
with its documented `uvicorn` command on `127.0.0.1:8001`) and the local PostgreSQL development
container. Every run used `python -m simulator` from the simulator's Python 3.12 virtual
environment, with the root `.env` loaded and scenario variables overridden in the process
environment only. No proxy environment variables were set.

Evidence sources: the simulator's stdout/stderr and exit code, the `test_results` row count in
PostgreSQL, and the Results API access log. Existing rows were never deleted; the positive runs
intentionally added synthetic rows.

| Scenario | Expected | Observed | Result |
|---|---|---|---|
| **A** `once`, correct key, batch 10 | exit 0, 10 result lines, 1 summary, +10 rows | exit 0; 10 result lines; `Batch done: 10 sent \| 10 accepted \| 0 rejected \| 0 undelivered \| 8 PASS \| 2 FAIL`; rows 3 → 13 (+10), all `source = simulator`; API log: 10 × `POST … 201`; stderr empty | ✅ |
| **B** `once`, wrong key, batch 10 | exit 1, 10 REJECTED lines, no retries, +0 rows | exit 1; 10 × `REJECTED: HTTP 401 (invalid or missing API key)`; 0 retry warnings; `0 accepted \| 10 rejected`; rows +0; API log: 10 × `POST … 401`; neither key appears in any output | ✅ |
| **C** `once`, unreachable URL (no listener), batch 1 | 3 attempts with 1 s + 2 s backoff, UNDELIVERED, exit 1 | exit 1; 2 retry warnings (`ConnectError`, attempts 1/3 and 2/3) + `UNDELIVERED: ConnectError after 3 attempts` = 3 total attempts. Each warning is logged after its backoff wait, just before the retry; the two warnings are 2.0 s apart (the 2 s backoff). Total run time about 3.3 s including process start, consistent with 1 s + 2 s backoff (connections were refused immediately); `1 undelivered`; rows +0 | ✅ |
| **D** `loop`, batch 1, interval 30 s, `SIGTERM` after first summary | exit 0, prompt stop, no second batch | PID verified as `python -m simulator`; exit 0 about 0.14 s after the signal; 1 result line, 1 summary, no zero-result summary; stderr empty | ✅ |
| **D2** same with `SIGINT` | exit 0, prompt stop, no second batch | PID verified as `python -m simulator`; exit 0 about 0.14 s after the signal; 1 result line, 1 summary, no zero-result summary; stderr empty | ✅ |
| **E** `once`, `LOG_FORMAT=json`, batch 2 | every line valid JSON with structured fields | exit 0; 3/3 lines parsed with Python's `json` (2 results + summary); `service = simulator`; result fields `device_id`, `test_name`, `verdict`; summary counters present; `°C` literal; stderr empty | ✅ |
| **F** `SIM_SEED=123`, batch 5, run twice | identical business output | exit 0 both; outputs **byte-for-byte identical** (no normalisation needed); stored `started_at` differs (real time) | ✅ |
| **G** `SIM_FAILURE_RATE=2`, sentinel key | exit 2, variable named, no request, key hidden | exit 2; `SIM_FAILURE_RATE: Input should be less than or equal to 1`; sentinel key absent; API log: 0 new lines | ✅ |

Spec §12 M3 criteria: `SIM_MODE=once` populates the database, prints one line per result and a
batch summary, and exits 0 (A); with a wrong API key it prints REJECTED lines and exits 1 (B).

---

## Known operational notes / M4 backlog

Full M4 backlog: GitHub issue #3.

- **No request idempotency.** Ambiguous transport failures after sending (`ReadTimeout`,
  `ReadError`, `WriteTimeout`, `WriteError`, `RemoteProtocolError`) are not retried, because the
  server may already have stored the row; they are reported as `UNDELIVERED`. HTTP `5xx`
  responses are still retried (see [Retry behaviour](#retry-behaviour)), so a narrow duplicate
  risk remains if the server commits the row and then returns a `5xx`.
- **No circuit breaker.** During a Results API outage every generated result goes through its own
  retry sequence, so a batch can take a long time.
- **Proxy settings.** The HTTP client uses httpx's default behaviour (`trust_env`), so standard
  `HTTP(S)_PROXY` environment variables may affect connections. An explicit proxy policy can be
  considered during hardening.
- **Signals.** Only `loop` mode handles `SIGTERM`/`SIGINT` gracefully; `once` mode deliberately
  installs no handlers.
- **Containers (future platform work).** When the simulator is containerised, the behaviour of the
  process as PID 1 (signal delivery, reaping) needs to be reviewed, e.g. an init process such as
  `tini`, `docker run --init` or Compose `init: true`, depending on the final design. Not
  implemented yet.
- **No HTTP endpoints.** As a CLI, the simulator has no `/health`, `/ready`, `/metrics` or `/docs`;
  those platform-contract items apply to the APIs only (spec §2).
