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
how to run and operate it and records the M3 verification evidence and the M4 platform-contract
review.

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

**PID 1 caveat (spec §8.3.3; M4 item N5).** A process running as **PID 1** (e.g. as a container's
main process without an init process) ignores signals that have the default action. In `once`
mode, SIGTERM therefore has no effect until the platform sends SIGKILL. A container running `once`
mode needs an init process (e.g. `tini` or `docker run --init`); this is part of the future
container design (platform work), not of the simulator code.

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

The Results API commits as its last database step, which removed the known path in which reading
back an already stored row could turn it into a `5xx` (N2, fixed in the Results API, `99716dc`).
**Residual duplicate risk (accepted, D1):** if the COMMIT succeeds in the database but its
confirmation is lost, the Results API answers with a retryable `5xx`, and the simulator's retry can
store the result a second time (spec §8.3.2).

**Proxy settings (spec §8.3.2; M4 item D10).** The HTTP client is created explicitly with
`httpx.Client(trust_env=True)`, so the standard proxy environment variables (`HTTP_PROXY`,
`HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`) are intentionally honoured. If a proxy is set in the
environment and the Results API must be reached directly (e.g. inside a cluster), its host must be
listed in `NO_PROXY`. There is no separate simulator setting for this. httpx errors that are in
neither list of the table above (e.g. a `ProxyError` when a proxy refuses an HTTPS tunnel) are
treated as unexpected errors (exit code `3`; accepted, N9).

**No circuit breaker (accepted; spec §8.3.2, M4 item D11).** During a Results API outage every
generated result goes through its own retry sequence, so a batch can take a long time. Figures from
spec §8.3.2:

| Scenario | Per result | Batch of 10 (default) | Batch of 1000 (maximum) |
|---|---|---|---|
| Connection-timeout outage: every attempt ends at the 3 s connect timeout | 3 × 3 s + 1 s + 2 s = **12 s** | 120 s ≈ **2 min** | 12,000 s ≈ **3.3 h** |
| Conservative slow-server scenario: every attempt uses the full connect (3 s), write (10 s) and read (10 s) timeouts and then gets a `5xx` | 3 × (3 + 10 + 10) s + 1 s + 2 s = **72 s** | 720 s = **12 min** | 72,000 s = **20 h** |

The second row is a conservative planning figure, not a hard upper bound (see spec §8.3.2 for
why). How often batches run and whether runs may overlap is controlled by the scheduler in the
platform layer (e.g. Kubernetes CronJob settings), not by the simulator.

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
number of tests will change as the code evolves. For M4 (still 375 tests; one test was renamed),
`test_one_http_client_is_created_with_explicit_trust_env_and_closed` asserts that the client is
constructed with exactly `trust_env=True` (D10); see [M4 verification](#m4-verification).

---

## M3 acceptance evidence (manual end-to-end, historical)

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

## Platform contract review (M4)

Application implementation reviewed through commit `94cc3ae` against spec §2, on 2026-10-22.
**Status** describes the application side. Deployment items (image, CronJob, Kubernetes) are
platform work; they are listed as follow-up and are not assessed here. Per spec §2, rules 1–4, 8
and 9 apply to the simulator; rules 5, 6, 7, 10 and 11 are API-only (the simulator runs no HTTP
server and has no database). Evidence types: *automated* (test names), *MANUAL M4* (2026-10-22,
see [M4 verification](#m4-verification)), *historical MANUAL M3* (2026-09-26, see above).

| # | Rule (spec §2) | Status | Evidence | Platform follow-up |
|---|---|---|---|---|
| 1 | All configuration via env vars | MET | `simulator/config.py`; no hard-coded host, port or key. Proxies only via the standard proxy variables (D10, see [Retry behaviour](#retry-behaviour)). HTTP timeouts and retries are specified constants (spec §8.3.2) | ConfigMap for non-secret values, Secret for `RESULTS_API_KEY`; set `NO_PROXY` if a proxy is injected and the Results API must be reached directly |
| 2 | Fail fast on bad config | MET | Automated: `test_missing_required_variable_exits_2_and_names_it`, `test_invalid_value_exits_2_and_names_it`, `test_invalid_configuration_exits_2_before_anything_starts` (exit `2`, the variable is named, never its value). Historical MANUAL M3 scenario G | – |
| 3 | Stateless | MET | No files written; no database; each run only sends results over HTTP | – |
| 4 | Logs to stdout, secrets never logged, format selectable | MET | `simulator/logging_config.py` (all lines are records of `simulator.results` or other loggers on stdout); automated: `test_no_secret_leaks_into_logs_output_or_result`, `test_api_key_is_a_secret_and_hidden_in_repr`, text/json logging tests. Historical MANUAL M3 scenarios B, E | Log collection from stdout |
| 5 | `/health`, `/ready` | N/A (API-only, spec §2) | No HTTP server | – |
| 6 | `/metrics` | N/A (API-only, spec §2) | No HTTP server | – |
| 7 | Migrations separate | N/A (API-only, spec §2) | No database | – |
| 8 | Graceful shutdown on SIGTERM (loop mode) | MET | `loop`: SIGTERM/SIGINT only set a stop flag, exit `0` (`test_signal_handler_only_sets_the_stop_event`, `tests/unit/test_loop.py`; historical MANUAL M3 scenarios D, D2). `once`: no handler by design (`test_once_mode_installs_no_signal_handlers`, spec §8.3.3) | Init process for a `once` container running as PID 1 (N5); termination grace period long enough for an in-flight request |
| 9 | Build once, deploy everywhere | MET | No environment-specific code paths; only env vars differ (`APP_ENV` affects logs only) | One image; CronJob with overlap/runtime controls (e.g. `concurrencyPolicy: Forbid`, `activeDeadlineSeconds`), because there is no circuit breaker (D11) |
| 10 | Unique URL prefix | N/A (API-only, spec §2) | The simulator only calls `POST /api/v1/results` | – |
| 11 | Swagger UI `/docs` | N/A (API-only, spec §2) | No HTTP server | – |

## M4 verification

The application implementation was reviewed and tested through commit `94cc3ae`. These README
changes are documentation only.

### Automated (offline, run by Claude Code, 2026-10-22)

| Check | Result |
|---|---|
| `flake8` | no findings, exit 0 |
| `pytest -m "not integration"` | **375 passed**, no warnings, exit 0 |

### MANUAL (performed by Muhammad, 2026-10-22, local end-to-end run)

Against the real Results API (`127.0.0.1:8001`) and the local PostgreSQL development database, with
all standard upper- and lower-case proxy variables unset for this run:

| Check | Result |
|---|---|
| `SIM_MODE=once`, `SIM_BATCH_SIZE=5`, `RESULTS_API_URL=http://127.0.0.1:8001` | 5 result lines; `Batch done: 5 sent \| 5 accepted \| 0 rejected \| 0 undelivered \| 5 PASS \| 0 FAIL`; exit `0` |
| Stored by the Results API | `total` 28 → 33, ids 29–33, all `"source":"simulator"`; API log: 5 × `POST /api/v1/results … 201` |
| Read by the Reporting API (as `evp_reader`) | `summary` total 5, passed 5, failed 0, `pass_rate_percent` 100.0; hour bucket `2026-10-22T09:00:00Z` 5/5/0 |

The optional manual proxy check (an unusable `HTTP_PROXY` with and without `NO_PROXY`) was not
performed; D10 is covered by the unit test named under [Tests](#tests).

---

## Known limitations and M4 traceability

M4 findings are tracked in GitHub issue #3. IDs: `B#` = backlog item from issue #3, `N#` = new
finding of the M4 audit, `D#` = M4 decision.

### Resolved in M4 (Simulator)

| ID | Finding | Resolution | Commit |
|---|---|---|---|
| D10 | The proxy behaviour relied on httpx's implicit `trust_env` default | `httpx.Client(trust_env=True)` explicitly, locked by a unit test (spec §8.3.2) | `94cc3ae` |

Related change in the Results API (not a simulator finding): the post-commit read-back path that
could answer a stored result with a `5xx`, which the simulator then retried, was removed there (N2,
`99716dc`).

### Accepted risks and trade-offs

- **Residual duplicate risk (D1).** HTTP `5xx` is retried, so if the Results API's COMMIT succeeds
  but its confirmation is lost, a retry can store the result twice. After-send transport failures
  are not retried (see [Retry behaviour](#retry-behaviour)). Only request idempotency (not in
  scope) would remove the case.
- **No circuit breaker (D11).** A Results API outage makes a batch slow (see the figures under
  [Retry behaviour](#retry-behaviour)); scheduling limits are platform work (below).
- **Unlisted httpx errors (N9).** Transport errors outside both retry lists (e.g. `ProxyError`)
  end the run with exit code `3` without a batch summary, as specified for unexpected errors.
- **No signal handlers in `once` mode.** By design (spec §8.3.3): SIGTERM/SIGINT keep their default
  behaviour.
- **Configuration errors are plain text (N7).** The fail-fast message is printed before the log
  format is known, so it is plain text even with `LOG_FORMAT=json`.

### Deferred platform work (not implemented)

- **PID 1 in containers (N5).** A `once` container needs an init process (e.g. `tini`,
  `docker run --init` or Compose `init: true`, depending on the final design); see the PID 1 caveat
  under [Running once](#running-once).
- **`NO_PROXY` in the deployment (D10, deployment side).** If the platform injects a proxy and the
  Results API must be reached directly, the container/Kubernetes configuration must set `NO_PROXY`.
- **CronJob controls (D11, deployment side).** Prevent overlapping runs and bound the run time,
  e.g. `concurrencyPolicy: Forbid` and `activeDeadlineSeconds`.
- **Termination grace period.** It must allow an in-flight request to finish in `loop` mode
  (spec §8.3.3); to be verified during the Docker/Kubernetes phase.
