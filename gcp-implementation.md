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

## Layer 2 — GCP Cloud Tasks Queue

**When to implement:** If Layer 1 is still insufficient — multiple reps submit orders
concurrently during startup warmup, or BC raises its error rate above what the semaphore
can absorb.

### Architecture

```
Frontend  ──POST /bc/sales-orders/submit──────────►  bc-api
                                                      │
                                                      ├─ map line payloads
                                                      ├─ write task doc to Firestore (status: queued)
                                                      ├─ enqueue Cloud Task
                                                      └─ return { taskId, status: "queued" }  HTTP 202

Cloud Tasks ──dispatch──►  bc-api  POST /internal/tasks/process-order/{taskId}
                                    │
                                    ├─ validate X-Task-Secret header
                                    ├─ update Firestore (status: processing)
                                    ├─ POST header → BC (via _bc_request, semaphore-gated)
                                    ├─ POST each line → BC (sequential)
                                    ├─ update Firestore (status: done | failed)
                                    └─ return 503 on transient error → Cloud Tasks retries

Frontend  ──GET /tasks/{taskId}  every 3 s──►  bc-api reads Firestore → returns { status, result, error }
```

---

## Part A — Google Cloud Console Setup

> **Cost overview before you start**
>
> Both services used in Layer 2 have generous free tiers that cover typical order volumes:
>
> | Service | Free tier | Estimated usage at 100 orders/day |
> |---|---|---|
> | **Firestore** | 20,000 writes/day · 50,000 reads/day · 1 GiB storage | ~400 writes/day · ~500 reads/day — well within free |
> | **Cloud Tasks** | 1,000,000 tasks/month | ~3,000 tasks/month — well within free |
>
> If your project is on the **Blaze (pay-as-you-go) plan** (required for Cloud Run production
> use), the same free-tier quotas still apply — you are only charged when you exceed them.
> You will not be charged for normal order volumes.

---

### Step 1: Enable Required APIs

APIs must be enabled before any GCP service can be used.

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. At the very top of the page, click the **project selector dropdown** (shows your current project name). Confirm you are in the correct project before continuing.
3. In the left sidebar, click **APIs & Services**. A submenu appears — click **Library**.
4. You are now on the API Library page. In the search box at the top, type `Cloud Tasks` and press Enter.
5. Click the card titled **Cloud Tasks API** (published by Google).
6. On the Cloud Tasks API detail page, click the blue **Enable** button.
   - If the button says **Manage** instead, the API is already enabled — skip to step 8.
   - After clicking Enable, the page reloads and shows a dashboard. This means it's active.
7. Click the **← back arrow** in your browser to return to the API Library (or click **Library** in the left sidebar again).
8. In the search box, type `Cloud Firestore` and press Enter.
9. Click the card titled **Cloud Firestore API** (published by Google).
10. Click the blue **Enable** button (or confirm it already says **Manage**).

Both APIs are now enabled. Continue to Step 2.

---

### Step 2: Create the Firestore Database

> **Already have Firestore?** In the left sidebar, click **Firestore** → click the gear icon
> (**Settings**) → check the **Mode** field. If it says **Native**, skip this step entirely.
> If it says **Datastore**, you cannot use it for this — continue with the creation steps below
> to create a second database named `orders-db` in Native mode, then update `task_service.py`
> to pass `database="orders-db"` to `firestore.Client()`.

**Firestore free-tier details:**

| Quota | Free amount (per day unless noted) |
|---|---|
| Document reads | 50,000 |
| Document writes | 20,000 |
| Document deletes | 10,000 |
| Stored data | 1 GiB total |
| Network egress | 10 GiB/month |

Each order save uses approximately 3–4 writes (create task doc + 2 status updates) and
3–5 reads (frontend polling). At 100 orders/day this is ~400 writes and ~500 reads —
about 2% of the daily free quota.

**Creating the database:**

1. In the left sidebar, scroll down to the **Databases** section and click **Firestore**.
   - If you see a "Welcome to Firestore" splash page, click **Create database**.
   - If you see the Firestore data browser (you already have a database), confirm the mode as described in the note above.

2. On the **"Select a Cloud Firestore mode"** screen you will see two options:

   | Option | Description | Choose? |
   |---|---|---|
   | **Native mode** | "For new mobile and web apps and for use with server client libraries. Recommended." | ✅ **Select this one** |
   | **Datastore mode** | "For server apps with App Engine. Based on Cloud Datastore." | ❌ Do not select |

   Click the **Native mode** tile so it is highlighted, then click **Continue**.

3. On the **"Configure your database"** screen:

   **Database ID field:**
   - Leave this set to `(default)`.
   - The default database is the one eligible for the free quota. Named databases have separate billing and do not share the free-tier quota, so keeping `(default)` is the lower-cost option.

   **Location type — choose "Region" (not "Multi-region"):**

   | Option | Monthly cost | Availability | Recommendation |
   |---|---|---|---|
   | **Multi-region** (nam5, eur3) | Higher — stores data in multiple locations | Higher uptime SLA | Not needed here |
   | **Region** (single-region) | Lower — data stored in one location | Sufficient for this use case | ✅ **Select this** |

   Click **Region**.

   **Region dropdown:**
   - Select the **same region as your Cloud Run service**. To find your Cloud Run region: open a new browser tab → Cloud Run → click your bc-api service → look at the **Region** field on the details page (e.g. `asia-southeast1`).
   - Return to this tab and pick that same region from the dropdown.
   - Common choices: `asia-southeast1` (Singapore), `us-central1`, `europe-west1`.

   > **Important:** The Firestore region cannot be changed after the database is created. Make sure it matches your Cloud Run region.

4. Click **Create database** (blue button at the bottom right).

5. A loading spinner appears for 30–60 seconds while the database provisions.

6. When provisioning finishes, you are taken to the **Data** tab of your new Firestore database. You will see an empty panel with "Start a collection" as a prompt. This is correct — leave it empty. The bc-api creates the `order_tasks` collection automatically when the first order is submitted.

---

### Step 3: Create the Cloud Tasks Queue

> **Free tier:** The first 1,000,000 Cloud Tasks created per month are completely free.
> At 100 orders/day (~3,000 tasks/month) you will not incur any Cloud Tasks charges.

1. In the left sidebar, click **Cloud Tasks**. If you don't see it, type "Cloud Tasks" in the search bar at the top of the console and click the result.

2. On the Cloud Tasks page, click **Create queue** (blue button, top left).

3. The **"Create queue"** form appears. Fill in the **basic fields** at the top:

   | Field | What to enter |
   |---|---|
   | **Queue ID** | `bc-order-queue` |
   | **Region** | Pick the same region as your Cloud Run service from the dropdown (e.g. `asia-southeast1`) |

   > After selecting a region, a warning may appear: "Queue location cannot be changed after creation." That is expected — click past it.

4. Below the basic fields you will see a section labeled **"Show advanced settings"** with a downward arrow. Click it to expand. Two sub-sections appear: **Rate limits** and **Retry config**.

5. In the **Rate limits** sub-section, update these two fields:

   | Field | Default | Set to | Why |
   |---|---|---|---|
   | **Maximum concurrent dispatches** | 1000 | `4` | Caps simultaneous BC calls from Cloud Tasks at 4, matching the `_bc_semaphore` limit |
   | **Maximum dispatches per second** | 500 | `2` | Slows burst delivery during retry storms so BC isn't overwhelmed |

   Leave all other Rate limits fields at their defaults.

6. In the **Retry config** sub-section, update these fields:

   | Field | Default | Set to | Why |
   |---|---|---|---|
   | **Maximum number of attempts** | Unlimited | `5` | Stops Cloud Tasks from retrying indefinitely on permanent BC errors |
   | **Minimum backoff** | 0.1s | `2` | Seconds — initial wait before first retry |
   | **Maximum backoff** | 3600s | `60` | Seconds — longest wait between retries |
   | **Maximum doublings** | 16 | `4` | How many times the backoff doubles: 2s → 4s → 8s → 16s → 60s (capped) |

   > Backoff fields accept plain numbers in seconds. Type `2`, not `2s`.

7. Click **Create** (blue button at the bottom of the form).

8. You are returned to the Cloud Tasks queue list. Your `bc-order-queue` appears with status **Running**.

   Note the **Location** column value (e.g. `asia-southeast1`) — you will need the exact string for the `CLOUD_TASKS_LOCATION` environment variable.

---

### Step 4: Grant Permissions to the Cloud Run Service Account

The bc-api Cloud Run service runs under a **service account** — a GCP identity that controls which GCP APIs it can call. By default it cannot use Cloud Tasks or Firestore, so you need to grant those roles.

**Part 1 — Find the service account your Cloud Run service uses:**

1. In the left sidebar, click **Cloud Run**.
2. Click your bc-api service name in the list.
3. On the service detail page, click the **Security** tab (tab bar near the top: METRICS / LOGS / TRIGGERS / REVISIONS / DETAILS / SECURITY).
4. Under the **Service account** section you will see an email address in the format:
   - Default: `{project-number}-compute@developer.gserviceaccount.com`
   - Custom: could be any `something@{project-id}.iam.gserviceaccount.com`
5. Copy that email address. You will need it in Part 2.

**Part 2 — Add the two required roles:**

1. In the left sidebar, click **IAM & Admin**, then click **IAM** in the submenu.
2. You see a table listing all principals (accounts) and their roles. At the top of the table there is a filter/search box — type the email address you copied. The list filters down to one row.
3. On that row, click the **pencil icon** (Edit principal) on the far right.
4. A side panel opens titled **"Edit access"** showing the current roles. Click **Add another role** (blue text link near the bottom of the role list).
5. A dropdown search field appears. Type `Cloud Tasks Enqueuer` and select it from the list.
   - Full role name: `roles/cloudtasks.enqueuer`
   - Description: "Submits tasks to a queue."
6. Click **Add another role** again. In the new dropdown, type `Cloud Datastore User` and select it.
   - Full role name: `roles/datastore.user`
   - Description: "Provides read/write access to data in a Cloud Datastore database." This role also covers **Firestore in Native mode** — both use the same underlying Datastore API.
7. Click **Save** (blue button at the bottom of the panel).

The panel closes and you return to the IAM table. The service account row now shows both new roles listed alongside any existing ones.

---

### Step 5: Generate a Task Secret

The `TASK_SECRET` is a random string that the bc-api adds to every Cloud Task request header.
The `/internal/tasks/process-order` endpoint rejects any call that does not include this
exact string, preventing anyone from triggering order processing by calling the endpoint directly.

**Generate the secret:**

1. Open a new browser tab and go to **passwords.google.com/tools/password** (Google's built-in password tool), or use any of these alternatives:
   - **Bitwarden Generator**: bitwarden.com/password-generator → set length to 48, toggle off Symbols
   - **1Password Generator**: 1password.com/password-generator → length 48, Letters + Numbers only
   - **Random.org**: random.org/strings → Length 48, Digits + Uppercase + Lowercase

2. Generate a string that is:
   - At least **40 characters** long
   - Contains only **letters (A–Z, a–z) and numbers (0–9)**
   - **No symbols** (`!@#$%` etc.) — these can break environment variable parsing if not quoted correctly

3. **Copy the string** and store it somewhere safe (a password manager entry, your `.env.local` file). You will paste it into Cloud Run in the next step and will need it again when setting environment variables locally.

---

### Step 6: Add Environment Variables to Cloud Run

You need to add 5 new environment variables to the Cloud Run revision. These tell the bc-api where to find the queue and how to authenticate with it.

**First, collect the values you need:**

| Variable | Where to find it |
|---|---|
| `GCP_PROJECT_ID` | Top of any Google Cloud Console page — click the project name dropdown at the top-left. The **Project ID** is shown below the project name in the dropdown (it looks like `my-project-12345`, not the display name). |
| `CLOUD_TASKS_LOCATION` | From Step 3 — the **Location** column in the Cloud Tasks queue list (e.g. `asia-southeast1`). |
| `CLOUD_TASKS_QUEUE` | `bc-order-queue` (the queue ID you created in Step 3). |
| `BC_API_URL` | Your Cloud Run service URL. In Cloud Run → click your bc-api service → the URL appears at the top of the detail page (e.g. `https://bc-api-xxxx-as.a.run.app`). Copy it **without a trailing slash**. |
| `TASK_SECRET` | The string you generated in Step 5. |

**Add the variables:**

1. In the left sidebar, click **Cloud Run** → click your bc-api service.
2. Click the blue **Edit & Deploy New Revision** button (top right of the service page).
3. The revision editor opens. In the left-side panel, click **Variables & Secrets** (third section in the list).
4. You will see any existing environment variables listed. Click **Add variable** to add each of the 5 new ones:
   - Click **Add variable** → type the variable name in the **Name** field → paste the value in the **Value** field.
   - Repeat for all 5 variables. The table should look like:

   | Name | Value |
   |---|---|
   | `GCP_PROJECT_ID` | `my-project-12345` |
   | `CLOUD_TASKS_LOCATION` | `asia-southeast1` |
   | `CLOUD_TASKS_QUEUE` | `bc-order-queue` |
   | `BC_API_URL` | `https://bc-api-xxxx-as.a.run.app` |
   | `TASK_SECRET` | `(your 40+ character string from Step 5)` |

5. Do not change any other settings on this screen.
6. Scroll to the bottom and click **Deploy** (blue button).
7. Cloud Run creates a new revision and routes traffic to it. The progress bar at the top of the page turns green when the revision is healthy. This takes 1–2 minutes.
8. Confirm the new revision is serving: on the **Revisions** tab, the latest revision should show **100%** traffic and a green checkmark.

---

## Part B — Backend Code Changes

### 1. Add dependencies to `requirements.txt`

Add these two lines:

```
google-cloud-tasks==2.16.3
google-cloud-firestore==2.19.0
```

---

### 2. Add new env vars to `src/config.py`

Add to the bottom of `config.py`:

```python
# Layer 2 — Cloud Tasks async order queue
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
CLOUD_TASKS_LOCATION = os.getenv("CLOUD_TASKS_LOCATION", "")
CLOUD_TASKS_QUEUE = os.getenv("CLOUD_TASKS_QUEUE", "bc-order-queue")
BC_API_URL = os.getenv("BC_API_URL", "")
TASK_SECRET = os.getenv("TASK_SECRET", "")
```

---

### 3. Create `src/services/task_service.py` (new file)

```python
"""Cloud Tasks enqueue and Firestore task-result store for async order processing."""
import json
import logging
import time
import uuid

from google.cloud import firestore
from google.cloud import tasks_v2

from src import config

logger = logging.getLogger("task_service")

_tasks_client: tasks_v2.CloudTasksClient | None = None
_db: firestore.Client | None = None
_COLLECTION = "order_tasks"


def _tasks() -> tasks_v2.CloudTasksClient:
    global _tasks_client
    if _tasks_client is None:
        _tasks_client = tasks_v2.CloudTasksClient()
    return _tasks_client


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=config.GCP_PROJECT_ID)
    return _db


def enqueue_order(
    order_type: str,
    api_version: str,
    header: dict,
    lines: list,
    company: str,
) -> str:
    """Write a task document to Firestore and enqueue a Cloud Task to process the order.

    order_type  : "sales" or "returns"
    api_version : "v1" (RGMC v1 API) or "v2" (RGMC v2 API)
    header      : order header dict, already field-mapped and ready for BC
    lines       : list of line dicts, already field-mapped and ready for BC
    company     : BC company name

    Returns the task_id UUID string — the frontend polls GET /tasks/{task_id}.
    """
    task_id = str(uuid.uuid4())

    _firestore().collection(_COLLECTION).document(task_id).set({
        "status": "queued",
        "order_type": order_type,
        "created_at": time.time(),
        "result": None,
        "error": None,
    })

    queue_path = _tasks().queue_path(
        config.GCP_PROJECT_ID,
        config.CLOUD_TASKS_LOCATION,
        config.CLOUD_TASKS_QUEUE,
    )

    body = json.dumps({
        "task_id": task_id,
        "order_type": order_type,
        "api_version": api_version,
        "header": header,
        "lines": lines,
        "company": company,
    }).encode()

    _tasks().create_task(
        parent=queue_path,
        task={
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{config.BC_API_URL}/internal/tasks/process-order/{task_id}",
                "headers": {
                    "Content-Type": "application/json",
                    "X-Task-Secret": config.TASK_SECRET,
                },
                "body": body,
            }
        },
    )
    logger.info(f"Enqueued {order_type}/{api_version} order task {task_id}")
    return task_id


def get_task(task_id: str) -> dict | None:
    doc = _firestore().collection(_COLLECTION).document(task_id).get()
    return doc.to_dict() if doc.exists else None


def update_task(task_id: str, **fields):
    _firestore().collection(_COLLECTION).document(task_id).update(fields)
```

---

### 4. Create `src/routers/bc_routes/task_routes.py` (new file)

```python
"""Cloud Tasks HTTP callback and task-status polling endpoints."""
import logging

from fastapi import APIRouter, HTTPException, Request, status

from src import config
from src.services.bc_functions import (
    rgmc_create_record,
    rgmc_delete_record,
    rgmc_v2_create_record,
    rgmc_v2_delete_record,
)
from src.services.task_service import get_task, update_task

logger = logging.getLogger("task_routes")

task_router = APIRouter(tags=["Tasks"])


@task_router.post(
    "/internal/tasks/process-order/{task_id}",
    include_in_schema=False,  # hidden from Swagger
)
async def process_order(task_id: str, request: Request):
    """Cloud Tasks HTTP target — not for direct client use.

    Cloud Tasks calls this endpoint after the order is enqueued.
    Returns 503 on transient failures so Cloud Tasks retries automatically.
    Returns 200 (with ok=False) on permanent failures — error stored in Firestore.
    """
    if request.headers.get("X-Task-Secret", "") != config.TASK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    order_type: str = body.get("order_type", "sales")
    api_version: str = body.get("api_version", "v1")
    header: dict = body.get("header", {})
    lines: list = body.get("lines", [])
    company: str = body.get("company") or config.BC_COMPANY

    update_task(task_id, status="processing")

    if api_version == "v2":
        _TABLE = "salesOrders" if order_type == "sales" else "salesReturnOrders"
        _LINES_TABLE = "salesOrderLines" if order_type == "sales" else "salesReturnOrderLines"
        _create = rgmc_v2_create_record
        _delete = rgmc_v2_delete_record
    else:
        _TABLE = "salesOrders"
        _LINES_TABLE = "salesOrderLines"
        _create = rgmc_create_record
        _delete = rgmc_delete_record

    try:
        http_status, data = _create(_TABLE, header, company_name=company)
        if http_status not in (200, 201):
            raise ValueError(f"Order header failed: BC returned {http_status}: {data}")

        order_id = data.get("id")

        for i, line in enumerate(lines, start=1):
            lh, ld = _create(
                f"{_TABLE}({order_id})/{_LINES_TABLE}",
                line,
                company_name=company,
            )
            if lh not in (200, 201):
                try:
                    _delete(_TABLE, order_id, company_name=company)
                except Exception as del_err:
                    logger.error(f"Rollback failed for task {task_id}: {del_err}")
                raise ValueError(f"Line {i} failed (BC {lh}): {ld}. Order rolled back.")

        update_task(task_id, status="done", result=data)
        logger.info(f"Task {task_id} done — order {data.get('no') or order_id}")
        return {"ok": True}

    except ValueError as e:
        # Permanent failure (bad data, rollback) — store result, don't retry
        update_task(task_id, status="failed", error=str(e))
        logger.error(f"Task {task_id} permanent failure: {e}")
        return {"ok": False, "error": str(e)}

    except Exception as e:
        err_str = str(e)
        update_task(task_id, status="failed", error=err_str)
        logger.error(f"Task {task_id} transient failure: {e}")
        # 503 causes Cloud Tasks to retry with backoff
        if any(code in err_str for code in ("429", "502", "503", "timeout", "ConnectionError")):
            raise HTTPException(status_code=503, detail=err_str)
        return {"ok": False, "error": err_str}


@task_router.get("/tasks/{task_id}", summary="Poll async order task status")
def get_task_status(task_id: str):
    """Return the current status of an async order submission.

    Poll every 3 seconds. Stop when status is "done" or "failed".

    Response shape:
    - status: "queued" | "processing" | "done" | "failed"
    - result: BC order record (only when status == "done"), includes "no" and "id"
    - error:  failure message (only when status == "failed")
    """
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
```

---

### 5. Add async submit endpoint to `src/routers/bc_routes/sales_order_routes.py`

Add the import at the top of the file (after the existing imports):

```python
from src.services.task_service import enqueue_order
```

Add this new endpoint function to the bottom of the file (keep the existing sync `create_sales_order` endpoint untouched):

```python
@sales_order_router.post(
    "/submit",
    summary="Submit Sales Order (async via Cloud Tasks)",
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_sales_order_async(
    body: SalesOrderCreate,
    company: Optional[str] = Query(None),
):
    """Async version of order creation. Returns a taskId to poll instead of waiting for BC."""
    try:
        payload = body.model_dump(mode="json", exclude_none=True)
        lines = payload.pop("lines", [])
        mapped_lines = [_map_line_payload(line) for line in lines]
        task_id = enqueue_order("sales", "v1", payload, mapped_lines, company or config.BC_COMPANY)
        return {"taskId": task_id, "status": "queued"}
    except Exception as e:
        logger.error(f"Failed to enqueue sales order: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
```

---

### 6. Add async submit endpoint to `src/routers/bc_routes/rgmc_sales_return_order_v2_routes.py`

Add the import at the top of the file:

```python
from src.services.task_service import enqueue_order
```

Add this new endpoint function to the bottom of the file:

```python
@rgmc_sales_return_order_v2_router.post(
    "/submit",
    summary="Submit Sales Return Order (async via Cloud Tasks)",
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_sales_return_order_async(
    body: SalesReturnOrderCreate,
    company: Optional[str] = Query(None),
):
    """Async version of return order creation. Returns a taskId to poll instead of waiting for BC."""
    try:
        payload = body.model_dump(mode='json', exclude_none=True)
        if 'customerNumber' in payload:
            payload['sellToCustomerNo'] = payload.pop('customerNumber')
        lines = payload.pop('lines', [])
        mapped_lines = []
        for i, line in enumerate(lines, start=1):
            lp = _map_line_payload(line)
            lp["lineNo"] = i * 10000
            mapped_lines.append(lp)
        task_id = enqueue_order("returns", "v2", payload, mapped_lines, company or config.BC_COMPANY)
        return {"taskId": task_id, "status": "queued"}
    except Exception as e:
        logger.error(f"Failed to enqueue return order: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
```

---

### 7. Register `task_router`

**`src/routers/bc_routes/__init__.py`** — add at the bottom:

```python
from .task_routes import task_router
```

**`src/routers/__init__.py`** — add `task_router` to the import:

```python
from .bc_routes import (
    # ... existing imports ...
    task_router,
)
```

**`src/main.py`** — add `api.include_router(task_router)` after the other router registrations:

```python
api.include_router(task_router)
```

---

## Part C — Frontend Code Changes

### 1. Add async submit + poll to `src/services/api.service.ts`

Add these three methods to the `ApiService` object, alongside the existing `submitSalesOrder` and `submitSalesReturnOrder`:

```typescript
async submitSalesOrderAsync(payload: SalesOrderPayload): Promise<{ taskId: string }> {
  const res = await apiClient.post('/bc/sales-orders/submit', payload);
  return res.data; // { taskId, status: 'queued' }
},

async submitSalesReturnOrderAsync(payload: SalesReturnOrderPayload): Promise<{ taskId: string }> {
  const res = await apiClient.post('/bc/custom/v2/sales-return-orders/submit', payload);
  return res.data; // { taskId, status: 'queued' }
},

async pollTask(
  taskId: string,
  onProgress?: (status: string) => void,
  timeoutMs = 120_000,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 3000));
    const { data } = await apiClient.get(`/tasks/${taskId}`);
    onProgress?.(data.status);
    if (data.status === 'done') return data.result;
    if (data.status === 'failed') {
      throw new Error(data.error ?? 'Order processing failed');
    }
  }
  throw new Error('Order timed out after 2 minutes. Check your orders before retrying.');
},
```

---

### 2. Update `src/views/SubmitPage.vue` — replace `doSubmitSales` and `doSubmitReturns`

Replace the two existing async submit functions with these:

```typescript
async function doSubmitSales(customerNumber: string, remarks: string) {
  salesStatus.value = 'submitting';
  const isNoSales = session.value?.noSales ?? false;
  const payload: SalesOrderPayload = {
    customerNumber,
    ...(session.value?.postingDate ? { postingDate: session.value.postingDate } : {}),
    externalDocumentNumber: isNoSales ? 'No Sales' : (remarks || undefined),
    ...(session.value?.user?.displayName ? { submittedBy: session.value.user.displayName } : {}),
    lines: sessionStore.salesOrders.map((l) => ({
      itemNumber: l.itemNumber,
      description: l.description,
      quantity: l.quantity,
      unitPrice: l.srp,
      ...(l.discountType === 'percent'
        ? { discountPercent: l.discountValue }
        : { lineDiscountAmount: l.discountValue }),
    })),
  };
  try {
    const { taskId } = await ApiService.submitSalesOrderAsync(payload);
    const result = await ApiService.pollTask(taskId, (s) => {
      if (s === 'processing') showToast('BC is processing your sales order…', 'primary');
    });
    salesSeriesNo.value = (result as Record<string, string>)?.no ?? (result as Record<string, string>)?.series ?? '';
    salesStatus.value = 'done';
    triggerSweep();
    showToast('Sales orders submitted!', 'success');
  } catch (err) {
    salesErrorObj.value = err instanceof Error ? err : new Error(String(err));
    salesError.value = salesErrorObj.value.message;
    salesStatus.value = 'failed';
    showToast('Sales submission failed. Will save locally.', 'danger');
  }
}

async function doSubmitReturns(customerNumber: string, remarks: string) {
  returnsStatus.value = 'submitting';
  const payload: SalesReturnOrderPayload = {
    customerNumber,
    ...(session.value?.postingDate ? { postingDate: session.value.postingDate } : {}),
    ...(remarks ? { externalDocumentNo: remarks } : {}),
    ...(session.value?.user?.displayName ? { submittedBy: session.value.user.displayName } : {}),
    lines: sessionStore.returnOrders.map((l) => ({
      itemNumber: l.itemNumber,
      description: l.description,
      quantity: l.quantity,
      unitPrice: l.srp,
      ...(l.discountType === 'percent'
        ? { discountPercent: l.discountValue }
        : { lineDiscountAmount: l.discountValue }),
    })),
  };
  try {
    const { taskId } = await ApiService.submitSalesReturnOrderAsync(payload);
    const result = await ApiService.pollTask(taskId, (s) => {
      if (s === 'processing') showToast('BC is processing your return order…', 'primary');
    });
    returnsSeriesNo.value = (result as Record<string, string>)?.no ?? (result as Record<string, string>)?.series ?? '';
    returnsStatus.value = 'done';
    triggerSweep();
    showToast('Return orders submitted!', 'success');
  } catch (err) {
    returnsErrorObj.value = err instanceof Error ? err : new Error(String(err));
    returnsError.value = returnsErrorObj.value.message;
    returnsStatus.value = 'failed';
    showToast('Return submission failed. Will save locally.', 'danger');
  }
}
```

---

## Part D — Deployment Checklist

Run through this after all code changes are committed and pushed:

- [ ] `requirements.txt` updated with `google-cloud-tasks` and `google-cloud-firestore`
- [ ] `src/config.py` has all five new env vars
- [ ] `src/services/task_service.py` created
- [ ] `src/routers/bc_routes/task_routes.py` created
- [ ] `/submit` endpoints added to `sales_order_routes.py` and `rgmc_sales_return_order_v2_routes.py`
- [ ] `task_router` registered in `bc_routes/__init__.py`, `routers/__init__.py`, and `main.py`
- [ ] GCP console: Cloud Tasks API and Firestore API enabled
- [ ] GCP console: Firestore database created (Native mode, correct region)
- [ ] GCP console: `bc-order-queue` Cloud Tasks queue created (correct region, `maxConcurrentDispatches=4`)
- [ ] GCP console: Cloud Run service account has **Cloud Tasks Enqueuer** + **Cloud Datastore User** roles
- [ ] GCP console: Cloud Run env vars set (`GCP_PROJECT_ID`, `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`, `BC_API_URL`, `TASK_SECRET`)
- [ ] Cloud Run revision deployed and healthy
- [ ] Frontend: `submitSalesOrderAsync`, `submitSalesReturnOrderAsync`, `pollTask` added to `api.service.ts`
- [ ] Frontend: `doSubmitSales` and `doSubmitReturns` updated in `SubmitPage.vue`

**Verify it works:**
1. Submit a small test order (1 line) and check the Firestore console → `order_tasks` collection for a document with `status: done`.
2. Submit an order while BC warmup is running to confirm Cloud Tasks queues it and retries without user error.
3. Check Cloud Run logs for `Task {id} done` log line.

---

## Quick reference

| Concern | Solution | Status |
|---|---|---|
| Concurrent BC reads flooding BC | `_bc_semaphore` in `_fetch_all_pages` | ✅ Done (Layer 1) |
| Write functions had no 429 retry | `_bc_request()` with retry on all writes | ✅ Done (Layer 1) |
| Warmup threads competing with saves | All warmup paths go through `_bc_semaphore` | ✅ Done (Layer 1) |
| Multiple reps saving simultaneously | Cloud Tasks queue + Firestore polling | ⬜ Pending (Layer 2) |
| Multiple Cloud Run instances sharing BC limit | `maxConcurrentDispatches=4` on Cloud Tasks queue | ⬜ Pending (Layer 2) |
