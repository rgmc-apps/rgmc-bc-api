# BC API — Rate Limit Handling & GCP Queue Architecture

## Problem

Business Central returns `429 Application_TooManyRequests` when the API client exceeds
5 concurrent requests or fills BC's 100-request waiting queue.

During an order save, the bc-api fires N+1 sequential BC calls (1 POST for the header
+ 1 POST per order line). Combined with background warmup threads that can be running
simultaneously, the process can easily push 6–8 concurrent BC requests — over BC's limit.
Previously, write functions (`bc_create_record`, `rgmc_v2_create_record`, etc.) had zero
retry logic, so any 429 propagated straight to the user as a 502.

---

## Layer 1 — In-Process Throttling (IMPLEMENTED)

**File:** `src/services/bc_functions.py`

### What changed

**A. Global BC concurrency semaphore**

```python
_bc_semaphore = threading.Semaphore(4)  # one below BC's 5-connection hard limit
```

Every outgoing BC call — reads, writes, and background warmup threads — must acquire a
slot before sending the request. If all 4 slots are busy the caller blocks in-process
until one frees. This prevents BC from ever seeing more than 4 simultaneous connections
from this process.

**B. `_bc_request()` — unified single-call helper with retry**

```python
def _bc_request(method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
```

Wraps every non-paginated BC call (single-record GETs and all writes). It:
1. Acquires `_bc_semaphore`
2. Increments `_active_bc_requests`
3. Retries on 429/502/503 up to 3 times, honouring `Retry-After` on 429s and using
   exponential backoff (1s → 2s → 4s, capped at 16s) for 5xx errors
4. Returns the final `requests.Response` regardless of outcome — callers check
   `status_code` / `raise_for_status()` as before

**C. `_fetch_all_pages()` — semaphore for paginated reads**

The outer `_fetch_all_pages` now wraps `_bc_semaphore` around the entire pagination
chain (one slot for the whole multi-page fetch), so background list and v3-price
warmup threads are also subject to the concurrency cap.

`_fetch_all_pages_inner` continues to call `_session.get` directly because the semaphore
slot is already held by the caller.

### Call sites updated

All of the following previously called `_session.*` directly with no rate limiting:

| Function | Method |
|---|---|
| `bc_get/create/update/delete_record` | GET / POST / PATCH / DELETE |
| `rgmc_get/create/update/delete_record` | GET / POST / PATCH / DELETE |
| `rgmc_get/update_contact_picture` | GET / PATCH |
| `rgmc_list/add/delete_contact_brand_tags` | GET / POST / DELETE |
| `rgmc_v2_get/create/update/delete_record` | GET / POST / PATCH / DELETE |
| `rgmc_v2_get/create/update/delete_item_price` | GET / POST / PATCH / DELETE |
| `rgmc_v2_get/create/update/delete_customer` | GET / POST / PATCH / DELETE |
| `rgmc_v2_get/update_contact_picture` | GET / PATCH |
| `rgmc_v2_list/add/delete_contact_brand_tags` | GET / POST / DELETE |
| `rgmc_v2_get/update_company_setting` | GET / PATCH |
| `rgmc_v3_get_item_price` | GET |
| `call_business_central_api` (companies list) | GET |

Two `_session.*` calls intentionally bypassed:
- `_session.post(BC_AUTH_URL, ...)` — OAuth token endpoint, not a BC data call
- `_session.get(..., timeout=120)` in `_fetch_all_pages_inner` — already inside the semaphore

### Effect on save flow

Before: order with 10 lines → 11 BC calls fire immediately → 429 if 5 slots are busy.

After: each BC call acquires a semaphore slot. If 4 are busy the 5th blocks locally
(no network round-trip). On retry the backoff is coordinated rather than every
thread independently hammering BC.

### Gunicorn/concurrency note

`gunicorn_config.py` runs 3 workers × 2 threads = 6 concurrent request slots.
With `Semaphore(4)`, even if all 6 slots try to hit BC simultaneously, only 4
will proceed — the other 2 queue in-process. The overall latency for the queued
requests is at most as long as the slowest in-flight BC call (120 s timeout), which
is far better than a 429 that the user must retry manually.

---

## Layer 2 — GCP Cloud Tasks Queue (NOT YET IMPLEMENTED)

**When to implement:** If Layer 1 is still insufficient — e.g., multiple reps submit
orders concurrently during the same warmup window, or BC raises its error rate.

### Architecture

```
Frontend  ──POST /bc/custom/v2/sales-orders──►  bc-api
                                                 │
                                                 ├─ enqueue Cloud Task (payload = order JSON)
                                                 └─ return { taskId, status: "queued" }  HTTP 202

Cloud Tasks ──dispatch──►  bc-api  /internal/tasks/process-order/{taskId}
                                    │
                                    ├─ acquire _bc_semaphore
                                    ├─ POST header → BC
                                    ├─ POST each line → BC (sequential)
                                    ├─ store result in Firestore: { taskId → { status, orderId, error } }
                                    └─ Cloud Tasks retries on 429 automatically

Frontend  ──GET /tasks/{taskId}──►  bc-api reads Firestore → returns result
```

### GCP services required

| Service | Role |
|---|---|
| **Cloud Tasks** | Enqueue order saves; `maxConcurrentDispatches=4` caps BC concurrency at the queue level |
| **Cloud Firestore** | Persist task results across Cloud Run instances and restarts |
| **Cloud Run `--concurrency`** | Lower per-instance concurrency (e.g. 2) so instances scale out rather than each instance flooding BC |

### Cloud Tasks queue config

```python
from google.cloud import tasks_v2

client = tasks_v2.CloudTasksClient()
queue = tasks_v2.Queue(
    name=queue_path,
    rate_limits=tasks_v2.RateLimits(
        max_concurrent_dispatches=4,   # never more than 4 simultaneous deliveries to bc-api
        max_dispatches_per_second=2,   # smooth burst on retry storms
    ),
    retry_config=tasks_v2.RetryConfig(
        max_attempts=5,
        min_backoff=duration_pb2.Duration(seconds=2),
        max_backoff=duration_pb2.Duration(seconds=60),
        max_doublings=4,
    ),
)
```

### Frontend changes required

- `submitOrder()` must handle `HTTP 202` with `{ taskId }` instead of the current `HTTP 201` with the order record.
- Add a polling loop (`GET /tasks/{taskId}` every 2–3 s, up to 120 s) with a progress indicator.
- The submit button stays disabled until `status === "done"` or `"failed"`.
- On `"failed"`, surface the error message from Firestore (same detail string as the current 502 response).

### Dependencies to add (bc-api)

```
google-cloud-tasks>=2.16
google-cloud-firestore>=2.16
```

### When NOT to implement

If reps never submit orders concurrently (the app is used sequentially by a single rep
per session), Layer 1 is sufficient. Cloud Tasks adds latency to every save and requires
significant frontend and backend work. Implement only if 429s persist after Layer 1.

---

## Quick reference

| Concern | Solution | Status |
|---|---|---|
| Concurrent BC reads flooding BC | `_bc_semaphore` in `_fetch_all_pages` | ✅ Done |
| Write functions had no 429 retry | `_bc_request()` with retry on all writes | ✅ Done |
| Warmup threads competing with saves | All warmup paths go through `_bc_semaphore` | ✅ Done |
| Multiple reps saving simultaneously | Cloud Tasks queue (Layer 2) | ⬜ Pending |
| Multiple Cloud Run instances sharing BC limit | Cloud Memorystore Redis semaphore | ⬜ Future |
