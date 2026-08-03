# Consignment Infrastructure — Resilient BC API on GCP

## Why Endpoints Still Fail After Layer 1

Layer 1 (in-process semaphore + retry) works correctly for a **single Cloud Run instance**.
The failures that survive Layer 1 come from three specific gaps:

### Gap 1 — Multi-instance semaphore escape (most likely cause)

`_bc_semaphore = threading.Semaphore(4)` is an **in-process lock**. It has no awareness of other
processes or other Cloud Run instances. Cloud Run auto-scales when request load builds up.
When two instances are running simultaneously:

```
Instance A: semaphore(4) → up to 4 concurrent BC connections
Instance B: semaphore(4) → up to 4 concurrent BC connections
                                                             ↓
Total BC connections possible: 8   (BC hard limit: 5)
```

Every concurrent BC connection above 5 produces a 429. BC queues up to 100 additional
requests before rejecting them — so a burst of concurrent sync requests from multiple
reps can overflow the queue entirely.

### Gap 2 — Cold-start catalog burst competes with live traffic

When Cloud Run starts a new instance, `rgmc_v3_warmup` fires immediately and launches
3 parallel BC catalog range requests (`_V3_CATALOG_RANGES`). If an old instance is
still serving traffic (during the Cloud Run rollout window), BC simultaneously receives:

```
New instance warmup:  3 parallel range requests (hold 3 semaphore slots)
Old instance traffic: up to 4 concurrent requests (hold 4 semaphore slots)
                                                             ↓
Total: 7 concurrent BC connections during every deploy
```

### Gap 3 — In-memory cache evaporated on every deploy

`_item_price_v3_cache` is a plain Python dict. Every Cloud Run revision update, OOM
kill, or instance restart wipes it entirely. The next request after any restart triggers
the full 6–20 s BC catalog fetch even if the data was fresh 10 seconds ago.

---

## Solution Architecture

Four changes, each independently deployable, all within GCP's free tier except one:

```
                    ┌─────────────────────────────────────────────────────┐
Sales Reps (PWA) → │  Cloud Run  (min=1, max=1 instance)                 │
                    │  ┌──────────────────┐  ┌────────────────────────┐  │
                    │  │  Read requests   │  │  Order submissions     │  │
                    │  │  ↓               │  │  ↓                     │  │
                    │  │  In-memory cache │  │  Cloud Tasks queue     │  │
                    │  │  hit → instant   │  │  ← 202 taskId           │  │
                    │  │  miss →          │  │  Firestore task status  │  │
                    │  │    GCS (<1s)     │  │  Poll GET /tasks/{id}   │  │
                    │  │    then BC (bg)  │  └────────────────────────┘  │
                    │  └──────────────────┘                               │
                    │                     semaphore(4) gates all BC calls │
                    └────────────────────────────────────────────────────-┘
                              │                         │
                    ┌─────────▼──────┐       ┌─────────▼──────┐
                    │  Cloud Storage │       │  Business       │
                    │  catalog cache │       │  Central API    │
                    │  (GCS backup)  │       │  (≤5 concurrent)│
                    └────────────────┘       └────────────────-┘
                              ▲
                    ┌─────────┴──────┐
                    │ Cloud Scheduler│
                    │ 6 AM daily     │
                    │ catalog refresh│
                    └────────────────┘
```

---

## Cost Overview

| Component | What it does | Monthly cost |
|---|---|---|
| Cloud Run min-instances=1 | Keep one instance warm; eliminates cold starts | ~$2–3/month |
| Cloud Run max-instances=1 | Single instance; semaphore works globally | FREE (config only) |
| Cloud Storage bucket | Catalog JSON persistence across deploys | < $0.01/month |
| Cloud Scheduler | Daily 6 AM pre-warm job | FREE (3 jobs/month free) |
| Cloud Tasks queue | Async order submission queue | FREE (< 1M tasks/month) |
| Firestore | Order task status tracking | FREE (< 50K reads/day) |
| **Total additional** | | **~$2–3/month** |

---

## Layer 2A — Cloud Run Instance Constraints

**What this fixes:** Gaps 1 and 2 (multi-instance escape + cold-start burst). Immediate.
**Cost:** FREE (configuration only) + ~$2–3/month for min-instances=1.

### GCP Console Setup

1. In the left sidebar, click **Cloud Run**.
2. Click your bc-api service name.
3. Click the blue **Edit & Deploy New Revision** button (top right).
4. In the revision editor, click **Capacity** in the left-side panel (it may also be labeled **Resources**).

   **Set these four fields:**

   | Field | Current (likely default) | Set to | Why |
   |---|---|---|---|
   | **Minimum number of instances** | 0 | `1` | Keeps one instance warm — no cold starts, no warmup bursts |
   | **Maximum number of instances** | 100 (default) | `1` | Only one instance ever runs; semaphore(4) is a global BC limit |
   | **Concurrency** | 80 (default) | `6` | Matches Gunicorn's actual capacity (3 workers × 2 threads) |
   | **CPU allocation** | CPU allocated during requests | Leave unchanged | Don't change to "CPU always allocated" — that increases cost |

   > **Why max-instances=1 is safe here:** A single Cloud Run instance with Gunicorn 3×2
   > handles 6 concurrent HTTP requests. Real usage is 2–4 reps syncing simultaneously, each
   > sending 1–2 requests at a time. The bottleneck is BC (5 connections), not Cloud Run's
   > request capacity. Scaling to 2 instances doesn't help — it just doubles BC connections.

5. Do not change memory, CPU, or timeout settings.
6. Scroll to the bottom and click **Deploy**.
7. Wait for the new revision to show **100% traffic** and a green checkmark on the **Revisions** tab.

**Verify it worked:** On the Cloud Run service detail page, under **Capacity**, you should see
*Min instances: 1* and *Max instances: 1*.

---

## Layer 2B — Cloud Storage Catalog Persistence

**What this fixes:** Gap 3 (cache wipe on deploy/restart). After any restart, the catalog
loads from GCS in < 1 second instead of fetching from BC (6–20 seconds).
**Cost:** < $0.01/month.

### Part A: GCP Console Setup

#### Step 1: Enable the Cloud Storage API

1. Go to **APIs & Services** → **Library** in the left sidebar.
2. Search for `Cloud Storage` and click the **Cloud Storage API** card.
3. Click **Enable** if not already enabled. If it says **Manage**, it is already enabled — skip to Step 2.

#### Step 2: Create the catalog bucket

1. In the left sidebar, click **Cloud Storage** → **Buckets**.
2. Click **Create** (blue button, top left).
3. Fill in the bucket creation form:

   **Name your bucket:**
   - Enter a globally unique name: `rgmc-bc-catalog-{your-project-id}` (replace `{your-project-id}`
     with your GCP project ID — visible in the dropdown at the top of the console).
   - Example: `rgmc-bc-catalog-my-project-12345`
   - Write this name down — you will need it for the environment variable.

   Click **Continue**.

   **Choose where to store your data:**

   | Option | Select? | Why |
   |---|---|---|
   | Multi-region | No | More expensive; not needed |
   | Dual-region | No | More expensive; not needed |
   | **Region** | **Yes** | Cheapest; pick same region as your Cloud Run service |

   In the **Region** dropdown: select the same region as your Cloud Run service (e.g. `asia-southeast1`).

   Click **Continue**.

   **Choose a storage class:**
   - Select **Standard**. The catalog is a small file read and written once daily — Standard is cheapest for this access pattern.

   Click **Continue**.

   **Choose how to control access to objects:**
   - Leave **"Prevent public access"** checked (default).
   - Under **Access control**, select **Uniform** (recommended by Google).

   Click **Continue**.

   **Choose how to protect object data:**
   - Leave all settings at their defaults.

   Click **Create**.

4. The bucket detail page appears. You will see it is empty. That is correct — the bc-api
   writes to it automatically when the first catalog fetch completes.

#### Step 3: Grant Cloud Run service account access to the bucket

1. In the left sidebar, click **IAM & Admin** → **IAM**.
2. Find your Cloud Run service account (format: `{project-number}-compute@developer.gserviceaccount.com`).
   If you don't know it: Cloud Run → click your service → Security tab → copy the service account email.
3. Click the **pencil icon** (Edit principal) on the service account row.
4. Click **Add another role** → search `Storage Object User` → select it.
   - Full role name: `roles/storage.objectUser`
   - This allows the bc-api to read and write objects in the bucket.
5. Click **Save**.

#### Step 4: Add the bucket name as an environment variable

1. Cloud Run → click your bc-api service → **Edit & Deploy New Revision**.
2. Click **Variables & Secrets** → **Add variable**.
3. Add:

   | Name | Value |
   |---|---|
   | `GCS_CATALOG_BUCKET` | `rgmc-bc-catalog-{your-project-id}` (the bucket name from Step 2) |

4. Click **Deploy**.

### Part B: Backend Code Changes

#### 1. Add dependency to `requirements.txt`

```
google-cloud-storage==2.19.0
```

#### 2. Add env var to `src/config.py`

Add to the bottom of `config.py`:

```python
# Layer 2B — Cloud Storage catalog persistence
GCS_CATALOG_BUCKET = os.getenv("GCS_CATALOG_BUCKET", "")
```

#### 3. Create `src/services/gcs_catalog.py` (new file)

```python
"""Cloud Storage-backed persistence for the v3 item price catalog.

Reads and writes are called from background threads in bc_functions.py,
never from request handlers — so latency doesn't affect the user.
"""
import json
import logging
import time

from src.config import GCS_CATALOG_BUCKET

logger = logging.getLogger("gcs_catalog")

_client = None


def _gcs():
    global _client
    if _client is None:
        from google.cloud import storage
        _client = storage.Client()
    return _client


def _blob_path(company_name: str) -> str:
    return f"catalogs/{company_name.upper()}/latest.json"


def load_catalog(company_name: str) -> dict | None:
    """Load the persisted catalog from GCS.

    Returns {"records": list, "on_date": str, "saved_at": float} or None.
    Returns None if GCS_CATALOG_BUCKET is not set, the object doesn't exist,
    or any GCS error occurs (non-fatal — BC fetch continues as normal).
    """
    if not GCS_CATALOG_BUCKET:
        return None
    try:
        blob = _gcs().bucket(GCS_CATALOG_BUCKET).blob(_blob_path(company_name))
        if not blob.exists():
            return None
        data = json.loads(blob.download_as_text(encoding="utf-8"))
        count = len(data.get("records", []))
        logger.info(f"GCS catalog loaded: {count} records (company={company_name}, date={data.get('on_date')})")
        return data
    except Exception as e:
        logger.warning(f"GCS catalog load failed (company={company_name}): {e}")
        return None


def save_catalog(company_name: str, on_date: str, records: list) -> None:
    """Persist the catalog to GCS after every successful BC fetch.

    Called from _rgmc_v3_fetch_and_cache (background thread) — never blocks
    request handling. Failure is non-fatal and logged.
    """
    if not GCS_CATALOG_BUCKET:
        return
    try:
        payload = json.dumps({"records": records, "on_date": on_date, "saved_at": time.time()})
        _gcs().bucket(GCS_CATALOG_BUCKET).blob(_blob_path(company_name)).upload_from_string(
            payload, content_type="application/json"
        )
        logger.info(f"GCS catalog saved: {len(records)} records (company={company_name}, date={on_date})")
    except Exception as e:
        logger.warning(f"GCS catalog save failed (company={company_name}): {e}")
```

#### 4. Edit `src/services/bc_functions.py` — two changes

**Change 1:** At the top of `bc_functions.py`, add the import after the existing imports:

```python
from src.services.gcs_catalog import load_catalog as _gcs_load, save_catalog as _gcs_save
```

**Change 2:** In `_rgmc_v3_fetch_and_cache`, after the line
`_item_price_v3_cache[cache_key] = ...` (line ~859), add a GCS save call:

```python
        _item_price_v3_cache[cache_key] = {"data": data, "expires_at": time.time() + _V3_CACHE_TTL}
        logger.info(f"v3 item prices cache refreshed: {len(records)} records (company={company_name})")
        # Persist to GCS so cold starts and deploys can skip the BC fetch.
        _gcs_save(company_name, on_date or datetime.date.today().isoformat(), records)
```

**Change 3:** In `rgmc_v3_warmup` (around line 1075), replace the entire function body
with a version that loads from GCS first before triggering the BC fetch:

Find the function (it looks like):
```python
def rgmc_v3_warmup(company_name: str):
    """..."""
    today = datetime.date.today().isoformat()
    ...
```

Replace the body with:
```python
def rgmc_v3_warmup(company_name: str):
    """Warm the v3 price cache. Loads from GCS first (< 1s) then refreshes from BC in background.

    Cold-start path with GCS:  GCS load → serve immediately → BC refresh in background.
    Cold-start path without GCS:  BC background fetch → serve after 6-20 s.
    """
    today = datetime.date.today().isoformat()
    full_key = (company_name, None, None, None, today, None)

    # Fast path: restore from GCS so the first user request doesn't wait for BC.
    gcs = _gcs_load(company_name)
    if gcs and gcs.get("records"):
        _purge_expired_v3_cache()
        _item_price_v3_cache[full_key] = {
            "data": {"value": gcs["records"]},
            "expires_at": time.time() + _V3_CACHE_TTL,
        }
        logger.info(
            f"v3 warmup: {len(gcs['records'])} records from GCS "
            f"(company={company_name}, cached_date={gcs.get('on_date')})"
        )

    # Always trigger a BC background refresh — GCS data may be from yesterday or an
    # old session. The background fetch updates the cache and writes fresh GCS data.
    _trigger_v3_refresh(full_key, company_name, None, None, None, today, None)
```

---

## Layer 2C — Cloud Scheduler Pre-warm

**What this fixes:** Ensures the catalog is fresh in GCS and in memory before reps
start their day. Even if Cloud Run was idle overnight (memory cleared by a restart),
the 6 AM job fetches from BC and writes to GCS — so the first rep to sync at 8 AM
hits the GCS cache, not BC.
**Cost:** FREE (first 3 Cloud Scheduler jobs/month are free).

### GCP Console Setup

#### Step 1: Enable the Cloud Scheduler API

1. Go to **APIs & Services** → **Library**.
2. Search `Cloud Scheduler` → click the card → click **Enable**. (Skip if already enabled.)

#### Step 2: Create the daily pre-warm job

1. In the left sidebar, click **Cloud Scheduler**.
2. Click **Create job** (blue button, top left).
3. Fill in the form:

   **Define the schedule:**

   | Field | Value | Notes |
   |---|---|---|
   | **Name** | `bc-catalog-prewarm` | Any name works |
   | **Region** | Same as your Cloud Run service | e.g. `asia-southeast1` |
   | **Frequency** | `0 6 * * *` | 6:00 AM every day (cron syntax) |
   | **Timezone** | `Asia/Manila` | Or your reps' local timezone |

   Click **Continue**.

   **Configure the execution:**

   | Field | Value | Notes |
   |---|---|---|
   | **Target type** | HTTP | Not Pub/Sub |
   | **URL** | `https://your-bc-api-url.a.run.app/bc/custom/v3/item-prices/refresh?company=YOUR_COMPANY_CODE` | Replace with your Cloud Run URL and company code |
   | **HTTP method** | POST | |
   | **Auth header** | Add OIDC token | Described below |

   **Setting up the OIDC auth header:**
   - Click **Add OIDC token** in the Auth header section.
   - In the **Service account** dropdown: select your Cloud Run service account
     (the same `{project-number}-compute@developer.gserviceaccount.com` from Layer 2A).
   - Leave **Audience** blank — Cloud Scheduler fills it automatically from the URL.

   > The OIDC token lets Cloud Scheduler call your Cloud Run service without making
   > the endpoint public. Cloud Run validates the token automatically.

4. Click **Create**.
5. On the Cloud Scheduler job list, find `bc-catalog-prewarm` and click **Run now** to test it.
   - The **Last run result** column should show **Success** within 30 seconds.
   - Check Cloud Run logs: you should see `v3 item prices cache refreshed` and `GCS catalog saved`.

#### Step 3: Grant Cloud Run invocation permission to the scheduler service account

1. In the left sidebar, click **Cloud Run** → click your bc-api service.
2. In the **Permissions** tab (or IAM sidebar), click **Add principal**.
3. In the **New principals** field, enter the service account you used for the scheduler
   (the same `{project-number}-compute@developer.gserviceaccount.com`).
4. Role: select **Cloud Run Invoker** (`roles/run.invoker`).
5. Click **Save**.

> If your Cloud Run service already allows unauthenticated invocations (the default for
> most deployed services), you can skip Step 3. Check under Cloud Run → your service →
> **Triggers** tab — if it says **Allow unauthenticated invocations**, skip this step.

---

## Layer 2D — Cloud Tasks for Order Submissions

**What this fixes:** Order submissions that fail when BC is busy (429) or warming up (503).
Instead of the frontend waiting 30–60 s for all order lines to post to BC, the submission
returns immediately with a task ID and the frontend polls for completion.

This layer is fully documented in `gcp-implementation.md` (Part A through Part D).
Implement that document in full after Layers 2A–2C are stable.

**Priority order:** 2A first (immediate BC relief, no code needed), then 2B (GCS cache,
~30 lines of new code), then 2C (scheduler, no code), then 2D (Cloud Tasks, larger change).

---

## Deployment Checklist

### Layer 2A
- [ ] Cloud Run **Minimum instances** set to `1`
- [ ] Cloud Run **Maximum instances** set to `1`
- [ ] Cloud Run **Concurrency** set to `6`
- [ ] New revision deployed and serving 100% traffic with green checkmark

### Layer 2B
- [ ] `google-cloud-storage==2.19.0` added to `requirements.txt`
- [ ] `GCS_CATALOG_BUCKET` added to `src/config.py`
- [ ] `src/services/gcs_catalog.py` created
- [ ] `_gcs_save` import added to `bc_functions.py`
- [ ] `_rgmc_v3_fetch_and_cache` calls `_gcs_save` after writing to `_item_price_v3_cache`
- [ ] `rgmc_v3_warmup` loads from GCS before triggering BC refresh
- [ ] GCS bucket created in the correct region
- [ ] Cloud Run service account has **Storage Object User** role on the bucket
- [ ] `GCS_CATALOG_BUCKET` env var added to Cloud Run
- [ ] New revision deployed
- [ ] Verify: trigger a sync, then check the GCS bucket console — `catalogs/{COMPANY}/latest.json` should appear

### Layer 2C
- [ ] Cloud Scheduler API enabled
- [ ] `bc-catalog-prewarm` job created with correct URL and timezone
- [ ] **Run now** tested successfully (Last run result: Success)
- [ ] Cloud Run logs show `GCS catalog saved` after the test run
- [ ] Cloud Run Invoker role granted if service requires authentication

### Layer 2D
- [ ] See `gcp-implementation.md` Part A–D deployment checklist

---

## Verifying It Works

**After Layer 2A:**
- Open Cloud Run → your service → **Metrics** tab. Under **Instance count**, the graph
  should show a flat line at 1 (never 0, never 2+). If it dips to 0, min-instances wasn't saved.
- Submit orders simultaneously from two browser sessions (different reps). Both should succeed.
  Previously they would race and one would get a 429.

**After Layer 2B:**
- Deploy a new revision (even without code changes — just re-deploy the same image).
- Immediately hit the sync endpoint. The Cloud Run logs should show
  `v3 warmup: X records from GCS` within 1–2 seconds (not after 6–20 s from BC).
- Verify the GCS bucket contains `catalogs/{COMPANY}/latest.json`.

**After Layer 2C:**
- Click **Run now** on the Cloud Scheduler job.
- Cloud Run logs show `v3 item prices cache refreshed` then `GCS catalog saved`.
- The GCS file's `saved_at` timestamp updates.

---

## Quick Reference

| Problem | Root cause | Fix | Cost |
|---|---|---|---|
| 429 on concurrent saves | Multiple Cloud Run instances each bypass semaphore | `max-instances=1` | FREE |
| 429 during new instance startup | Warmup burst + live traffic exceed 5 BC connections | `max-instances=1` | FREE |
| Cold-start delays (6–20s) for users | In-memory cache empty after restart | GCS catalog backup | ~$0 |
| Cold starts on every deploy | Cloud Run scales to 0 between requests | `min-instances=1` | ~$2–3/mo |
| Catalog stale after midnight | No refresh until first user triggers it | Cloud Scheduler 6 AM | FREE |
| Order submission fails under BC load | Synchronous BC call during high traffic | Cloud Tasks queue | FREE |
